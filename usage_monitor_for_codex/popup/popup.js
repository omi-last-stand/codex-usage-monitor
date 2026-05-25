let els;
let statusState = {};
let translations = {};
let textTimerId = null;
let pressScreenPos = null;

/**
 * Set CSS custom properties for theme colors and inject translation strings.
 *
 * Called once by Python after the page loads.  Translations are set as
 * textContent on heading elements so the HTML file stays language-neutral.
 *
 * @param {object} config - { colors, t (translations), app_version, data (initial snapshot) }
 */
function init(config) {
    const s = document.documentElement.style;
    for (const [key, value] of Object.entries(config.colors)) {
        s.setProperty(`--${key.replaceAll('_', '-')}`, value);
    }

    translations = config.t;
    document.getElementById('title').textContent = translations.title;
    document.getElementById('headingAccount').textContent = translations.account;
    document.getElementById('labelEmail').textContent = translations.email;
    document.getElementById('labelPlan').textContent = translations.plan;
    document.getElementById('headingExtraUsage').textContent = translations.extra_usage;
    document.getElementById('headingClaudeCode').textContent = translations.claude_code;

    const changelogLink = document.getElementById('changelogLink');
    changelogLink.textContent = translations.changelog;
    changelogLink.addEventListener('click', () => pywebview.api.open_url());
    document.getElementById('closeBtn').addEventListener('click', () => pywebview.api.close());

    document.getElementById('appVersion').textContent = config.app_version;

    document.body.classList.add('widget');
    // Restore the last compact/expanded view; default to compact on first run.
    if (!config.expanded) {
        document.body.classList.add('compact');
    }
    // Whole window is draggable (easy_drag). Distinguish a real click from
    // the click that ends a drag, so moving the window doesn't also toggle.
    document.body.addEventListener('pointerdown', onWidgetPointerDown);
    document.body.addEventListener('click', onWidgetClick);
    setupContextMenu(config.always_on_top !== false);

    els = {
        blocks: document.getElementById('blocks'),
        emailRow: document.getElementById('emailRow'),
        emailValue: document.getElementById('emailValue'),
        planRow: document.getElementById('planRow'),
        planValue: document.getElementById('planValue'),
        extraSpent: document.getElementById('extraSpent'),
        extraPct: document.getElementById('extraPct'),
        extraFill: document.getElementById('extraFill'),
        installRows: document.getElementById('installRows'),
        statusSection: document.getElementById('statusSection'),
        statusText: document.getElementById('statusText'),
    };

    updateData(config.data);
    requestAnimationFrame(() => document.body.classList.add('open'));
}

/**
 * Toggle between the compact and expanded view in widget mode.
 *
 * Clicks on interactive controls (close button, changelog link) are
 * ignored so those keep their own behavior.
 *
 * @param {MouseEvent} event
 */
function onWidgetPointerDown(event) {
    pressScreenPos = { x: event.screenX, y: event.screenY };
}

function onWidgetClick(event) {
    if (event.target.closest('#closeBtn') || event.target.closest('#changelogLink') || event.target.closest('#contextMenu')) {
        return;
    }
    // Ignore the click that ends a drag: if the pointer moved more than a few
    // pixels between press and release, treat it as a move, not a toggle.
    if (pressScreenPos) {
        const dx = Math.abs(event.screenX - pressScreenPos.x);
        const dy = Math.abs(event.screenY - pressScreenPos.y);
        pressScreenPos = null;
        if (dx > 4 || dy > 4) {
            return;
        }
    }
    document.body.classList.toggle('compact');
    pywebview.api.set_expanded(!document.body.classList.contains('compact'));
}

/**
 * Wire up the right-click context menu (widget mode only):
 * always-on-top toggle, settings, about, quit.
 *
 * @param {boolean} alwaysOnTop - Initial always-on-top state, used to set
 *   the menu checkmark to match the value restored from the INI file.
 */
function setupContextMenu(alwaysOnTop) {
    const menu = document.getElementById('contextMenu');
    const aotCheck = document.querySelector('#menuAlwaysOnTop .menu-check');
    aotCheck.textContent = alwaysOnTop ? '✓' : '';

    document.querySelector('#menuAlwaysOnTop .menu-label').textContent = translations.menu_always_on_top;
    document.querySelector('#menuSettings .menu-label').textContent = translations.menu_settings;
    document.querySelector('#menuAbout .menu-label').textContent = translations.menu_about;
    document.querySelector('#menuQuit .menu-label').textContent = translations.menu_quit;

    document.body.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        menu.classList.add('open');
        menu.style.left = `${Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 4)}px`;
        menu.style.top = `${Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 4)}px`;
    });
    document.addEventListener('click', () => menu.classList.remove('open'));

    document.getElementById('menuAlwaysOnTop').addEventListener('click', () => {
        Promise.resolve(pywebview.api.toggle_always_on_top()).then((on) => {
            aotCheck.textContent = on ? '✓' : '';
        });
    });
    document.getElementById('menuSettings').addEventListener('click', () => pywebview.api.open_settings());
    document.getElementById('menuAbout').addEventListener('click', () => pywebview.api.show_about());
    document.getElementById('menuQuit').addEventListener('click', () => pywebview.api.quit_app());
}

/**
 * Close the right-click context menu. Called from Python on an outside click.
 */
function closeContextMenu() {
    const menu = document.getElementById('contextMenu');
    if (menu) {
        menu.classList.remove('open');
    }
}

/**
 * Update all popup sections with fresh data from Python.
 *
 * @param {object} data - Pre-formatted snapshot from _snapshot_to_dict().
 */
function updateData(data) {
    if (data.profile) {
        els.emailValue.textContent = data.profile.email;
        els.emailRow.style.display = data.profile.email ? '' : 'none';
        els.planValue.textContent = data.profile.plan;
        els.planRow.style.display = data.profile.plan ? '' : 'none';
    }

    reconcileBars(data.usage || []);

    if (data.extra) {
        els.extraSpent.textContent = data.extra.spent_text;
        els.extraPct.textContent = data.extra.pct_text;
        els.extraFill.style.width = `${data.extra.fill_pct * 100}%`;
    }

    if (data.installations?.length) {
        els.installRows.replaceChildren(...data.installations.map((inst) => {
            const row = document.createElement('div');
            const dt = document.createElement('dt');
            dt.textContent = inst.name;
            const dd = document.createElement('dd');
            dd.textContent = inst.version;
            row.append(dt, dd);
            return row;
        }));
    }

    updateStatus(data.status);
    applyLayout(data.layout || []);
}

/**
 * Add, update, or remove the usage-bar blocks inside #blocks by key, leaving the
 * singleton blocks (account, extra, versions, status) untouched. Ordering and
 * visibility are applied separately by applyLayout().
 */
function reconcileBars(entries) {
    const wanted = new Set(entries.map((entry) => entry.key));
    for (const bar of [...els.blocks.querySelectorAll('.usage-entry')]) {
        if (!wanted.has(bar.dataset.block)) bar.remove();
    }
    for (const entry of entries) {
        const existing = els.blocks.querySelector(`.usage-entry[data-block="${entry.key}"]`);
        if (existing) {
            updateBarElement(existing, entry);
        } else {
            const bar = createBarElement(entry);
            els.blocks.appendChild(bar);
            const fill = bar.querySelector('.bar-fill');
            requestAnimationFrame(() => { fill.style.width = `${entry.fill_pct * 100}%`; });
        }
    }
}

/**
 * Order and show/hide every block from the saved layout. The layout lists the
 * visible and collapsed blocks in display order; a block not in it (hidden, or
 * with no data) is dropped from the flex flow.
 *
 * @param {Array<{key: string, state: string}>} layout
 */
function applyLayout(layout) {
    const config = new Map();
    layout.forEach((block, index) => config.set(block.key, { state: block.state, index }));
    for (const el of els.blocks.children) {
        const entry = config.get(el.dataset.block);
        if (entry) {
            el.style.order = entry.index;
            el.classList.add('shown');
            el.classList.toggle('state-collapsed', entry.state === 'collapsed');
        } else {
            el.classList.remove('shown');
        }
    }
}

/**
 * Update the status footer with live timer data or static text.
 *
 * Live mode (has last_success_time): starts a 1-second interval for
 * the text counter.  Static mode (has text): shows plain text.
 */
function updateStatus(status) {
    if (textTimerId) {
        clearInterval(textTimerId);
        textTimerId = null;
    }

    if (!status) {
        statusState = {};
        return;
    }

    if (status.last_success_time !== undefined) {
        statusState = {
            lastSuccessTime: status.last_success_time,
            nextPollTime: status.next_poll_time,
            refreshing: status.refreshing,
            error: status.error,
        };
        els.statusSection.classList.toggle('error', !!status.error);
        tickStatusText();
        textTimerId = setInterval(tickStatusText, 1000);
    } else {
        statusState = {};
        els.statusText.textContent = status.text || '';
        els.statusSection.classList.toggle('error', !!status.is_error);
    }
}

/**
 * Build and display the status text from current state.
 *
 * < 60s:  "Updated Xs ago"
 * >= 60s: "Updated Xm ago · Next update in Ym"
 * + refreshing or error appended with · separator
 */
function tickStatusText() {
    if (!statusState.lastSuccessTime) return;

    const now = Date.now() / 1000;
    const secondsAgo = Math.max(0, Math.floor(now - statusState.lastSuccessTime));
    const isStale = !!statusState.nextPollTime && (now > statusState.nextPollTime + 30);
    els.blocks.classList.toggle('stale', isStale);

    const parts = [formatDuration(secondsAgo)];

    if (statusState.refreshing) {
        parts.push(translations.status_refreshing);
    } else if (statusState.error) {
        parts.push(statusState.error);
    } else if (secondsAgo >= 60 && statusState.nextPollTime) {
        const secondsUntil = Math.max(0, Math.floor(statusState.nextPollTime - now));
        if (secondsUntil > 0) {
            parts.push(translations.status_next_update.replace('{duration}', formatCountdown(secondsUntil)));
        }
    }

    els.statusText.textContent = parts.join(' \u00b7 ');
}

/**
 * Format seconds into a localized "Updated Xs ago" / "Updated Xm ago" string.
 */
function formatDuration(totalSeconds) {
    if (totalSeconds < 60) {
        return translations.status_updated_s.replace('{s}', totalSeconds);
    }

    const totalMin = Math.floor(totalSeconds / 60);
    const hours = Math.floor(totalMin / 60);
    const mins = totalMin % 60;

    let duration;
    if (hours > 0) {
        duration = translations.duration_hm.replace('{h}', hours).replace('{m}', mins);
    } else {
        duration = translations.duration_m.replace('{m}', totalMin);
    }
    return translations.status_updated.replace('{duration}', duration);
}

/**
 * Format a countdown in seconds into a localized duration string.
 */
function formatCountdown(totalSeconds) {
    if (totalSeconds < 60) {
        return translations.duration_s.replace('{s}', totalSeconds);
    }

    const totalMin = Math.ceil(totalSeconds / 60);
    const hours = Math.floor(totalMin / 60);
    const mins = totalMin % 60;

    if (hours > 0) {
        return translations.duration_hm.replace('{h}', hours).replace('{m}', mins);
    }
    return translations.duration_m.replace('{m}', totalMin);
}

function createBarElement(entry) {
    const div = document.createElement('div');
    div.className = 'usage-entry block';
    div.dataset.block = entry.key || '';

    const header = document.createElement('div');
    header.className = 'bar-header';
    const label = document.createElement('span');
    label.textContent = entry.label;
    const pct = document.createElement('span');
    pct.className = 'bar-pct';
    pct.textContent = entry.pct_text;
    header.append(label, pct);

    const container = document.createElement('div');
    container.className = 'bar-container';
    const fill = document.createElement('div');
    fill.className = 'bar-fill';
    fill.classList.toggle('warn', entry.warn);
    fill.style.width = '0%';
    container.appendChild(fill);

    for (const pos of entry.midnights) {
        const d = document.createElement('div');
        d.className = 'bar-divider';
        d.style.left = `calc(${pos * 100}% - 1px)`;
        container.appendChild(d);
    }

    if (entry.marker_rel !== null) {
        const marker = document.createElement('div');
        marker.className = 'bar-marker';
        marker.style.left = `calc(${entry.marker_rel * 100}% - 1px)`;
        container.appendChild(marker);
    }

    div.append(header, container);

    if (entry.reset_text) {
        const reset = document.createElement('div');
        reset.className = 'reset-text';
        reset.textContent = entry.reset_text;
        div.appendChild(reset);
    }

    return div;
}

function updateBarElement(div, entry) {
    div.querySelector('.bar-pct').textContent = entry.pct_text;

    const fill = div.querySelector('.bar-fill');
    fill.style.width = `${entry.fill_pct * 100}%`;
    fill.classList.toggle('warn', entry.warn);

    const container = div.querySelector('.bar-container');
    let marker = container.querySelector('.bar-marker');
    if (entry.marker_rel !== null) {
        if (!marker) {
            marker = document.createElement('div');
            marker.className = 'bar-marker';
            container.appendChild(marker);
        }
        marker.style.left = `${entry.marker_rel * 100}%`;
    } else if (marker) {
        marker.remove();
    }

    for (const d of container.querySelectorAll('.bar-divider')) d.remove();
    for (const pos of entry.midnights) {
        const d = document.createElement('div');
        d.className = 'bar-divider';
        d.style.left = `${pos * 100}%`;
        container.appendChild(d);
    }

    let resetEl = div.querySelector('.reset-text');
    if (entry.reset_text) {
        if (resetEl) {
            resetEl.textContent = entry.reset_text;
        } else {
            resetEl = document.createElement('div');
            resetEl.className = 'reset-text';
            resetEl.textContent = entry.reset_text;
            div.appendChild(resetEl);
        }
    } else if (resetEl) {
        resetEl.remove();
    }
}

// Report content height changes to the host (pywebview or dev.html iframe parent).
new ResizeObserver(() => {
    const height = document.body.scrollHeight;
    if (window.pywebview?.api?.report_height) {
        pywebview.api.report_height(height);
    }
}).observe(document.body);
