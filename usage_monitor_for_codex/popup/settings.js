let fieldListEl;
let draggedRow = null;
let t = {};

// Language codes mapped to their endonym (each shown in its own language).
const LANGUAGES = [
    ['en', 'English'],
    ['ja', '日本語'],
    ['de', 'Deutsch'],
    ['fr', 'Français'],
    ['es', 'Español'],
    ['pt-BR', 'Português (Brasil)'],
    ['it', 'Italiano'],
    ['ko', '한국어'],
    ['hi', 'हिन्दी'],
    ['id', 'Bahasa Indonesia'],
    ['zh-CN', '简体中文'],
    ['zh-TW', '繁體中文'],
    ['uk', 'Українська'],
];

/**
 * Initialize the settings window: apply theme colors and render the field rows.
 *
 * Called once by Python after the page loads.
 *
 * @param {object} config - { colors, fields: [{ key, label, state }] }
 */
function initSettings(config) {
    const s = document.documentElement.style;
    for (const [key, value] of Object.entries(config.colors)) {
        s.setProperty(`--${key.replaceAll('_', '-')}`, value);
    }

    t = config.t;
    document.getElementById('heading').textContent = t.heading;
    document.getElementById('hint').textContent = t.hint;
    document.getElementById('saveBtn').textContent = t.save;
    document.getElementById('cancelBtn').textContent = t.cancel;
    document.getElementById('emptyHint').textContent = t.empty;

    fieldListEl = document.getElementById('fieldList');

    if (!config.fields || config.fields.length === 0) {
        document.getElementById('emptyHint').hidden = false;
    } else {
        for (const field of config.fields) {
            fieldListEl.appendChild(createFieldRow(field));
        }
        updateRowConstraints();
    }

    document.getElementById('languageLabel').textContent = t.language;
    document.getElementById('languageHint').textContent = t.language_hint;
    const langSelect = document.getElementById('languageSelect');
    const systemOption = document.createElement('option');
    systemOption.value = '';
    systemOption.textContent = t.language_system;
    langSelect.appendChild(systemOption);
    for (const [code, name] of LANGUAGES) {
        const opt = document.createElement('option');
        opt.value = code;
        opt.textContent = name;
        langSelect.appendChild(opt);
    }
    langSelect.value = config.language || '';

    document.getElementById('saveBtn').addEventListener('click', onSave);
    document.getElementById('cancelBtn').addEventListener('click', () => pywebview.api.cancel());
}

/**
 * Build one draggable field row with "collapse" and "hide" checkboxes.
 *
 * @param {object} field - { key, label, state }
 * @returns {HTMLLIElement}
 */
function createFieldRow(field) {
    const li = document.createElement('li');
    li.className = 'field-row';
    li.draggable = true;
    li.dataset.key = field.key;

    const handle = document.createElement('span');
    handle.className = 'drag-handle';
    handle.textContent = '☰';  // trigram: a drag affordance

    const label = document.createElement('span');
    label.className = 'field-label';
    label.textContent = field.label;

    const collapseLabel = document.createElement('label');
    collapseLabel.className = 'chk';
    const collapse = document.createElement('input');
    collapse.type = 'checkbox';
    collapse.className = 'chk-collapse';
    collapse.checked = field.state === 'collapsed';
    collapseLabel.append(collapse, document.createTextNode(t.collapse));

    const hideLabel = document.createElement('label');
    hideLabel.className = 'chk';
    const hide = document.createElement('input');
    hide.type = 'checkbox';
    hide.className = 'chk-hide';
    hide.checked = field.state === 'hidden';
    hideLabel.append(hide, document.createTextNode(t.hide));

    hide.addEventListener('change', updateRowConstraints);
    collapse.addEventListener('change', updateRowConstraints);

    li.append(handle, label, collapseLabel, hideLabel);
    addDragHandlers(li);
    return li;
}

/**
 * Enforce the cross-row rules after any change:
 *  - "Hide" wins over "collapse": a hidden row's collapse box is disabled.
 *  - At least one block must stay "visible" (shown in the compact view), so
 *    when only one visible row is left, both its boxes are disabled - the last
 *    visible block can be neither hidden nor collapsed.
 */
function updateRowConstraints() {
    const rows = [...fieldListEl.children];
    const isVisible = (row) => !row.querySelector('.chk-hide').checked && !row.querySelector('.chk-collapse').checked;
    const visibleCount = rows.filter(isVisible).length;
    for (const row of rows) {
        const hide = row.querySelector('.chk-hide');
        const collapse = row.querySelector('.chk-collapse');
        const lockVisible = visibleCount === 1 && isVisible(row);

        if (hide.checked) collapse.checked = false;
        collapse.disabled = hide.checked || lockVisible;
        collapse.closest('.chk').classList.toggle('disabled', hide.checked || lockVisible);
        hide.disabled = lockVisible;
        hide.closest('.chk').classList.toggle('disabled', lockVisible);
    }
}

/**
 * Wire HTML5 drag-and-drop reordering on a row.
 */
function addDragHandlers(li) {
    li.addEventListener('dragstart', () => {
        draggedRow = li;
        li.classList.add('dragging');
    });
    li.addEventListener('dragend', () => {
        li.classList.remove('dragging');
        draggedRow = null;
        for (const row of fieldListEl.children) row.classList.remove('drag-over');
    });
    li.addEventListener('dragover', (event) => {
        event.preventDefault();
        if (!draggedRow || draggedRow === li) return;
        li.classList.add('drag-over');
    });
    li.addEventListener('dragleave', () => li.classList.remove('drag-over'));
    li.addEventListener('drop', (event) => {
        event.preventDefault();
        li.classList.remove('drag-over');
        if (!draggedRow || draggedRow === li) return;
        // Move the dragged row to the drop target, preserving drag direction.
        const rows = [...fieldListEl.children];
        if (rows.indexOf(draggedRow) < rows.indexOf(li)) {
            li.after(draggedRow);
        } else {
            li.before(draggedRow);
        }
    });
}

/**
 * Collect the current order and per-field states, then send them to Python.
 */
function onSave() {
    const fields = [...fieldListEl.children].map((li) => {
        const hidden = li.querySelector('.chk-hide').checked;
        const collapsed = li.querySelector('.chk-collapse').checked;
        const state = hidden ? 'hidden' : (collapsed ? 'collapsed' : 'visible');
        return { key: li.dataset.key, state };
    });
    const language = document.getElementById('languageSelect').value;
    pywebview.api.save({ fields, language });
}
