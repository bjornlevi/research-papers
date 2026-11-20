(() => {
  const form = document.getElementById("rag-editor");
  if (!form) return;
  const editor = document.getElementById("rag-passages");
  const advanced = document.getElementById("rag-advanced");
  const status = document.getElementById("rag-edit-status");
  const ranges = form.elements.namedItem("ranges");
  const terms = form.elements.namedItem("labels");
  const blocks = [...document.querySelectorAll("[data-rag-block]")];
  const pending = [...document.querySelectorAll("[data-rag-unassigned]")];
  const total = blocks.length;

  function element(tag, text) {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    return node;
  }

  function valid(ranges) {
    let start = 0;
    let end = 0;
    for (const [first, last] of ranges) {
      if (first <= start || first > end + 1 || last <= end || last > total) return false;
      [start, end] = [first, last];
    }
    return end === total;
  }

  function read() {
    const groups = [];
    if (ranges.value.trim()) {
      for (const range of ranges.value.split(",")) {
        const match = /^\s*([1-9][0-9]{0,6})(?:\s*-\s*([1-9][0-9]{0,6}))?\s*$/.exec(range);
        if (!match) return null;
        groups.push([Number(match[1]), Number(match[2] || match[1])]);
      }
      if (!valid(groups)) return null;
    }
    const starts = new Set(groups.map(([first]) => first));
    const labels = new Map();
    for (const line of terms.value.split("\n")) {
      if (!line.trim()) continue;
      const match = /^\s*([1-9][0-9]{0,6})\s*:\s*(.+?)\s*$/.exec(line);
      if (!match || !starts.has(Number(match[1])) || labels.has(Number(match[1]))) return null;
      labels.set(Number(match[1]), match[2]);
    }
    return { groups, labels };
  }

  function writeTerms(labels) {
    terms.value = [...labels].sort(([a], [b]) => a - b).map(([first, label]) => `${first}: ${label}`).join("\n");
  }

  function update(groups, labels, index, message) {
    if (!valid(groups)) {
      status.textContent = "That edit would conflict with an overlapping passage. Adjust the overlap under Advanced.";
      status.setAttribute("tabindex", "-1");
      status.focus();
      return;
    }
    ranges.value = groups.map(([first, last]) => first === last ? first : `${first}-${last}`).join(", ");
    writeTerms(labels);
    render(index);
    status.textContent = `${message} Preview or save when you are ready.`;
    editor.querySelectorAll(":scope > details > summary")[index]?.focus();
  }

  function button(text, action) {
    const control = element("button", text);
    control.type = "button";
    control.className = "secondary";
    control.addEventListener("click", action);
    return control;
  }

  function render(openIndex = -1) {
    editor.replaceChildren();
    const plan = read();
    if (!plan) {
      editor.append(element("p", "Correct the numbered ranges or extra terms under Advanced to use the passage editor."));
      advanced.open = true;
      return;
    }
    const { groups, labels } = plan;
    if (!groups.length) {
      editor.append(element("p", "This draft uses whole PDF pages, with no custom passages to edit."));
      if (editor.dataset.suggested) {
        editor.append(button("Start with suggested passages", () => {
          ranges.value = editor.dataset.suggested;
          render(0);
          editor.querySelector("summary")?.focus();
          status.textContent = "Suggested passages added to the draft. Live search is unchanged.";
        }));
      }
      return;
    }
    editor.append(element("p", "Open a passage to read it, split it at a break in the text, or join it with the next passage."));
    groups.forEach(([first, last], index) => {
      const card = element("details");
      const start = blocks[first - 1].dataset;
      const end = blocks[last - 1].dataset;
      const pages = start.page === end.page ? `PDF page ${start.page}` : `PDF pages ${start.page}-${end.page}`;
      card.append(element("summary", [start.section, start.excerpt, pages].filter(Boolean).join(" · ")));
      const input = element("input");
      input.type = "text";
      input.maxLength = 200;
      input.value = labels.get(first) || "";
      const field = element("label", "Extra search terms (optional)");
      field.append(input);
      input.addEventListener("input", () => {
        if (input.value.trim()) labels.set(first, input.value.trim());
        else labels.delete(first);
        writeTerms(labels);
        status.textContent = "";
      });
      card.append(field, element("p", "Add words someone might search for that the paper phrases differently. This does not edit the paper."));
      if (pending.length) {
        const select = element("select");
        const placeholder = element("option", "Choose saved terms");
        placeholder.value = "";
        select.append(placeholder);
        for (const entry of pending) {
          const value = entry.dataset.ragUnassigned;
          if ([...select.options].some(option => option.value === value)) continue;
          const option = element("option", value);
          option.value = value;
          select.append(option);
        }
        const choice = element("label", "Terms that need review");
        choice.append(select);
        card.append(choice, button("Attach saved terms", () => {
          if (!select.value) {
            status.textContent = "Choose saved terms to attach.";
            select.focus();
            return;
          }
          if (input.value.trim() && input.value.trim() !== select.value) {
            status.textContent = "This passage already has extra terms. Choose an empty passage or move its existing terms first.";
            input.focus();
            return;
          }
          input.value = select.value;
          labels.set(first, input.value);
          writeTerms(labels);
          status.textContent = "Terms attached in this draft. Save or apply to keep the attachment.";
          input.focus();
        }));
      }
      if (groups[index + 1]) {
        card.append(button("Join with next passage", () => {
          const [nextFirst, nextLast] = groups[index + 1];
          const joined = [...new Set([labels.get(first), labels.get(nextFirst)].filter(Boolean))].join("; ");
          if (Array.from(joined).length > 200) {
            status.textContent = "Shorten the extra search terms on these passages before joining them.";
            status.setAttribute("tabindex", "-1");
            status.focus();
            return;
          }
          const updated = new Map(labels);
          updated.delete(nextFirst);
          if (joined) updated.set(first, joined);
          const next = groups.slice();
          next.splice(index, 2, [first, nextLast]);
          update(next, updated, index, "Passages joined.");
        }));
      }
      function showText() {
        if (!card.open || card.dataset.rendered) return;
        card.dataset.rendered = "true";
        for (let number = first; number <= last; number++) {
          const source = blocks[number - 1];
          const part = element("div");
          part.append(element("p", `PDF page ${source.dataset.page}`),
                      source.querySelector(".paper-clear-text").cloneNode(true));
          if (number < last) {
            part.append(button("Split after this text", () => {
              const next = groups.slice();
              next.splice(index, 1, [first, number], [number + 1, last]);
              update(next, labels, index + 1, "Passage split. Existing search terms stay on the first part.");
            }));
          }
          card.append(part);
        }
      }
      card.open = index === openIndex;
      card.addEventListener("toggle", showText);
      showText();
      editor.append(card);
    });
  }

  advanced.open = false;
  editor.hidden = false;
  render();
  for (const field of [ranges, terms]) field.addEventListener("input", () => {
    status.textContent = "";
    render();
  });
})();
