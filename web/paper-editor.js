import { Editor } from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import { Markdown } from '@tiptap/markdown';
import { TableKit } from '@tiptap/extension-table';

const source = document.querySelector('#edit-text');
const form = source?.form;
const shell = document.querySelector('#paper-editor');
const status = document.querySelector('#editor-status');
const toolbar = document.querySelector('#editor-toolbar');
const visual = document.querySelector('#visual-editor');

if (source && form && shell && toolbar && visual && status) {
  let pages = [];
  let active = null;
  let visualMode = true;
  let changed = false;
  const wholeDocument = form.elements.scope.value === 'document';
  const buttons = [...toolbar.querySelectorAll('[data-command]')];

  function parsePages(text) {
    if (!wholeDocument) return [{ marker: '', text }];
    const matches = [...text.matchAll(/^\[\[PAGE [^\n]*\]\]\n/gm)];
    if (!matches.length || matches[0].index !== 0) {
      throw new Error('Page markers are missing. Keep every original marker unchanged and in order.');
    }
    return matches.map((match, index) => {
      let body = text.slice(match.index + match[0].length, matches[index + 1]?.index ?? text.length);
      if (index < matches.length - 1) {
        if (!body.endsWith('\n\n')) throw new Error('Keep two newlines before each page marker.');
        body = body.slice(0, -2);
      }
      return { marker: match[0], text: body };
    });
  }

  let originalMarkers = null;

  function updateToolbar() {
    for (const button of buttons) {
      const command = button.dataset.command;
      const mark = { bold: 'bold', italic: 'italic', bullets: 'bulletList', numbers: 'orderedList' }[command];
      if (mark) button.setAttribute('aria-pressed', String(Boolean(active?.isActive(mark))));
      if (command === 'undo' || command === 'redo') button.disabled = !active?.can()[command]();
    }
    const heading = toolbar.querySelector('[data-heading]');
    heading.value = String([1, 2, 3].find(level => active?.isActive('heading', { level })) || 0);
  }

  function syncSource() {
    if (!visualMode) return;
    source.value = pages.map(page => page.marker + (page.dirty ? page.editor.getMarkdown() : page.text)).join('\n\n');
  }

  function markChanged() {
    changed = true;
    status.textContent = 'Unsaved changes. Save a draft to retain your edits.';
  }

  function buildEditors() {
    const parsed = parsePages(source.value);
    if (parsed.some(page => /<\/?[A-Za-z][A-Za-z0-9-]*(?:\s[^>]*|\/?)>|<!--|!\[[^\]]*\]/.test(page.text))) {
      throw new Error('This draft contains HTML or image markup not supported by visual editing. Use Markdown source to preserve it.');
    }
    if (originalMarkers === null) originalMarkers = parsed.map(page => page.marker);
    if (parsed.length !== originalMarkers.length || parsed.some((page, index) => page.marker !== originalMarkers[index])) {
      throw new Error('Keep every original page marker unchanged and in order.');
    }
    for (const page of pages) page.editor.destroy();
    visual.replaceChildren();
    pages = parsed;
    pages.forEach((page, index) => {
      const section = document.createElement('section');
      section.className = 'visual-page';
      const label = document.createElement('p');
      label.className = 'meta';
      label.textContent = wholeDocument ? `Page ${index + 1}` : 'Page text';
      const element = document.createElement('div');
      section.append(label, element);
      visual.append(section);
      page.editor = new Editor({
        element,
        injectCSS: false,
        extensions: [
          StarterKit.configure({
            link: { openOnClick: false, autolink: false, defaultProtocol: 'https' },
            underline: false,
          }),
          Markdown.configure({ markedOptions: { breaks: true, gfm: true } }),
          TableKit,
        ],
        content: page.text,
        contentType: 'markdown',
        editorProps: { attributes: { role: 'textbox', 'aria-label': `Visual text for page ${index + 1}`, 'aria-multiline': 'true' } },
        onFocus: ({ editor }) => { active = editor; updateToolbar(); },
        onSelectionUpdate: updateToolbar,
        onUpdate: () => { page.dirty = true; syncSource(); markChanged(); updateToolbar(); },
      });
    });
    active = pages[0]?.editor;
    updateToolbar();
  }

  function showError(error) {
    for (const page of pages) page.editor?.destroy();
    pages = [];
    active = null;
    visualMode = false;
    source.hidden = false;
    visual.hidden = true;
    toolbar.hidden = true;
    document.querySelector('label[for="edit-text"]').hidden = false;
    document.querySelector('#editor-mode').textContent = 'Use visual editor';
    status.textContent = `Visual editor: ${error.message} Your source text is still available.`;
  }

  document.querySelector('#editor-options').hidden = false;
  try {
    buildEditors();
    source.hidden = true;
    visual.hidden = false;
    toolbar.hidden = false;
    document.querySelector('label[for="edit-text"]').hidden = true;
    status.textContent = 'Visual editing. Page boundaries are protected; formatting is saved as Markdown.';
  } catch (error) {
    visualMode = false;
    showError(error);
  }

  toolbar.addEventListener('mousedown', event => {
    if (event.target.closest('button')) event.preventDefault();
  });
  toolbar.addEventListener('click', event => {
    const button = event.target.closest('[data-command]');
    if (!button || !active) return;
    const chain = active.chain().focus();
    const command = button.dataset.command;
    const commands = { bold: 'toggleBold', italic: 'toggleItalic', bullets: 'toggleBulletList',
      numbers: 'toggleOrderedList', undo: 'undo', redo: 'redo', unlink: 'unsetLink' };
    if (command === 'link') {
      const field = toolbar.querySelector('[data-link-url]');
      let url;
      try {
        url = new URL(field.value);
        if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) {
          throw new Error('Use an HTTP(S) link without credentials.');
        }
      } catch {
        status.textContent = 'Enter a valid HTTP(S) URL without credentials to add a link.';
        return;
      }
      chain.extendMarkRange('link').setLink({ href: url.href }).run();
      field.value = '';
    } else if (commands[command]) {
      chain[commands[command]]().run();
    }
    updateToolbar();
  });
  toolbar.querySelector('[data-heading]').addEventListener('change', event => {
    if (!active) return;
    const level = Number(event.target.value);
    if (level) active.chain().focus().setHeading({ level }).run();
    else active.chain().focus().setParagraph().run();
  });
  document.querySelector('#editor-mode').addEventListener('click', event => {
    if (visualMode) {
      syncSource();
      visualMode = false;
    } else {
      try {
        buildEditors();
        visualMode = true;
      } catch (error) {
        showError(error);
        return;
      }
    }
    source.hidden = visualMode;
    visual.hidden = !visualMode;
    toolbar.hidden = !visualMode;
    event.target.textContent = visualMode ? 'Edit Markdown source' : 'Use visual editor';
    status.textContent = visualMode ? 'Visual editing; page boundaries are protected.' : 'Markdown source; keep page markers unchanged.';
  });
  for (const button of document.querySelectorAll('[data-editor-width]')) {
    button.addEventListener('click', () => {
      shell.style.width = button.dataset.editorWidth === 'full' ? '100%' : '48rem';
      for (const option of document.querySelectorAll('[data-editor-width]')) {
        option.setAttribute('aria-pressed', String(option === button));
      }
    });
  }
  source.addEventListener('input', markChanged);
  form.addEventListener('submit', event => {
    syncSource();
    if (source.value.length > source.maxLength) {
      event.preventDefault();
      status.textContent = `The draft exceeds the ${source.maxLength.toLocaleString()} character limit. Shorten it before saving.`;
      return;
    }
    changed = false;
  });
  window.addEventListener('beforeunload', event => {
    if (changed) {
      event.preventDefault();
      event.returnValue = '';
    }
  });
}
