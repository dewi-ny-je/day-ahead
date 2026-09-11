// CodeMirror 6 based editor for the v2 config, secrets and log pages.
//
// The pages render a plain <textarea> inside a [data-code-editor] wrapper so
// they keep working without JavaScript.  When this module runs the textarea is
// hidden, a CodeMirror editor takes its place and the document is written back
// into the textarea on submit, which keeps the existing form based routes in
// dao/webserver/app/v2/routes.py unchanged.
//
// The JSON editors get code folding (the reason for using CodeMirror at all),
// plus a linter so a syntax error is visible before the file is sent to the
// server instead of coming back as a red banner.

import {closeBrackets, closeBracketsKeymap} from '@codemirror/autocomplete'
import {defaultKeymap, history, historyKeymap} from '@codemirror/commands'
import {json, jsonParseLinter} from '@codemirror/lang-json'
import {
    bracketMatching,
    ensureSyntaxTree,
    foldable,
    foldEffect,
    foldGutter,
    foldKeymap,
    HighlightStyle,
    indentOnInput,
    syntaxHighlighting,
    syntaxTree,
    unfoldAll,
} from '@codemirror/language'
import {linter, lintGutter, lintKeymap} from '@codemirror/lint'
import {highlightSelectionMatches, search, searchKeymap} from '@codemirror/search'
import {EditorState} from '@codemirror/state'
import {
    drawSelection,
    dropCursor,
    EditorView,
    highlightActiveLine,
    highlightActiveLineGutter,
    highlightSpecialChars,
    keymap,
    lineNumbers,
} from '@codemirror/view'
import {tags} from '@lezer/highlight'

// Only class names, no colours: the palette lives in main.scss so that
// switching between the light and the dark Bootstrap theme is pure CSS.
const highlightStyle = HighlightStyle.define([
    {tag: tags.propertyName, class: 'cm-token-property'},
    {tag: [tags.string, tags.special(tags.string)], class: 'cm-token-string'},
    {tag: [tags.number, tags.bool, tags.null], class: 'cm-token-number'},
    {tag: [tags.punctuation, tags.separator, tags.bracket], class: 'cm-token-punctuation'},
    {tag: tags.comment, class: 'cm-token-comment'},
    {tag: tags.invalid, class: 'cm-token-invalid'},
])

// Parsing the whole document can take a while for a large options.json; give
// the parser enough time so the fold buttons act on a complete syntax tree.
const PARSE_TIMEOUT_MS = 5000

/**
 * Collect the value of every member of the outermost object or array, so that
 * "Collapse sections" folds `battery`, `scheduler`, ... exactly once instead of
 * folding every nested object as foldAll() would.
 */
function topLevelValues(state) {
    ensureSyntaxTree(state, state.doc.length, PARSE_TIMEOUT_MS)

    const root = syntaxTree(state).topNode.firstChild
    if (!root || (root.name !== 'Object' && root.name !== 'Array')) return []

    const values = []
    for (let child = root.firstChild; child; child = child.nextSibling) {
        const value = child.name === 'Property' ? child.lastChild : child
        if (value && (value.name === 'Object' || value.name === 'Array')) values.push(value)
    }
    return values
}

function foldSections(view) {
    const effects = []
    for (const value of topLevelValues(view.state)) {
        const line = view.state.doc.lineAt(value.from)
        const range = foldable(view.state, line.from, line.to)
        if (range) effects.push(foldEffect.of(range))
    }

    if (effects.length > 0) view.dispatch({effects})
}

/**
 * Report the first JSON syntax error, using the same parser as the server.
 * Returns null when the document is valid.
 */
function jsonError(view) {
    const [diagnostic] = jsonParseLinter()(view)
    if (!diagnostic) return null

    return {
        message: diagnostic.message,
        line: view.state.doc.lineAt(diagnostic.from).number,
        position: diagnostic.from,
    }
}

function createToolbar(view, wrapper) {
    const toolbar = document.createElement('div')
    toolbar.className = 'code-editor-toolbar btn-group btn-group-sm mb-2'

    const buttons = [
        ['bi-chevron-contract', 'Collapse sections', () => foldSections(view)],
        ['bi-chevron-expand', 'Expand all', () => unfoldAll(view)],
    ]

    for (const [icon, label, action] of buttons) {
        const button = document.createElement('button')
        button.type = 'button'
        button.className = 'btn btn-outline-secondary'
        button.title = label
        button.innerHTML = `<i class="bi ${icon}"></i> ${label}`
        button.addEventListener('click', () => {
            action()
            view.focus()
        })
        toolbar.append(button)
    }

    wrapper.prepend(toolbar)
}

/**
 * Keep the textarea in sync on submit and refuse to send invalid JSON.
 */
function connectForm(view, wrapper, textarea, isJson) {
    const form = textarea.form
    if (!form) return

    // Below the editor, next to the button that triggers the validation.
    const feedback = document.createElement('div')
    feedback.className = 'invalid-feedback d-block d-none'
    wrapper.append(feedback)

    form.addEventListener('submit', (event) => {
        textarea.value = view.state.doc.toString()
        if (!isJson) return

        const error = jsonError(view)
        feedback.classList.toggle('d-none', error === null)
        if (error === null) return

        feedback.textContent = `Invalid JSON on line ${error.line}: ${error.message}`
        event.preventDefault()
        view.dispatch({selection: {anchor: error.position}, scrollIntoView: true})
        view.focus()
    })
}

function createEditor(wrapper) {
    const textarea = wrapper.querySelector('textarea')
    if (!textarea) return

    const isJson = wrapper.dataset.codeEditor === 'json'
    const readOnly = textarea.readOnly || textarea.disabled

    // Composed by hand instead of using basicSetup, so a log file does not get
    // a fold gutter it can never use and JSON does not get an autocompletion
    // popup without any completions behind it.
    const extensions = [
        lineNumbers(),
        highlightActiveLineGutter(),
        highlightActiveLine(),
        highlightSpecialChars(),
        highlightSelectionMatches(),
        drawSelection(),
        dropCursor(),
        history(),
        search({top: true}),
        syntaxHighlighting(highlightStyle),
        EditorView.lineWrapping,
        EditorState.readOnly.of(readOnly),
        // readOnly alone keeps the content editable in the DOM, which puts a
        // caret in the log view and suggests it can be changed.
        EditorView.editable.of(!readOnly),
    ]

    // The JSON bindings come first, so that for example Backspace removes a
    // bracket pair instead of falling through to the default binding.
    const keybindings = [defaultKeymap, historyKeymap, searchKeymap]

    if (isJson) {
        extensions.push(
            json(),
            foldGutter(),
            bracketMatching(),
            indentOnInput(),
            linter(jsonParseLinter()),
            lintGutter(),
        )
        if (!readOnly) extensions.push(closeBrackets())
        keybindings.unshift(closeBracketsKeymap, foldKeymap, lintKeymap)
    }

    extensions.push(keymap.of(keybindings.flat()))

    const view = new EditorView({
        state: EditorState.create({doc: textarea.value, extensions}),
        parent: wrapper,
    })

    // Keep the textarea as the value carrier of the form, but out of sight.
    textarea.hidden = true

    if (isJson && !readOnly) createToolbar(view, wrapper)
    if (!readOnly) connectForm(view, wrapper, textarea, isJson)
}

export function initCodeEditors(root = document) {
    root.querySelectorAll('[data-code-editor]').forEach(createEditor)
}
