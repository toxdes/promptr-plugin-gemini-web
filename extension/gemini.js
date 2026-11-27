(() => {
  const EDITOR = '[contenteditable="true"][role="textbox"][aria-label*="prompt" i]';
  const PICKER = [
    'button[aria-label^="Open mode picker" i]',
    'button[aria-label^="Open model picker" i]',
    'button[aria-label^="Choose model" i]',
    'button[aria-label^="Model selector" i]',
    '[data-test-id="bard-mode-menu-button"]',
  ].join(",");
  const MENUS = '[role="menu"], [role="listbox"], [role="dialog"]';
  const CHOICES = '[role="menuitem"], [role="menuitemradio"], [role="option"], button';
  const CONTROL = 'button, a, [role="button"]';

  class GeminiError extends Error {}

  function visible(element) {
    return element.getClientRects().length > 0 && getComputedStyle(element).visibility === "visible";
  }

  function findVisible(root, selector, predicate = () => true) {
    return Array.from(root.querySelectorAll(selector)).find(el => visible(el) && predicate(el));
  }

  function label(element) {
    return (element.getAttribute("aria-label") || element.innerText || "").trim();
  }

  function modelId(name) {
    return name.toLowerCase() === "auto" ? "auto" : name;
  }

  function enabled(element) {
    return !element.matches(':disabled, [aria-disabled="true"]');
  }

  async function waitFor(find, message, timeout = 7000) {
    const deadline = Date.now() + timeout;
    do {
      const value = find();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 100));
    } while (Date.now() < deadline);
    throw new GeminiError(message);
  }

  function requireAccount() {
    if (findVisible(document, CONTROL, el => /^sign in$/i.test(label(el)))) {
      throw new GeminiError("Sign in to Gemini in the Chrome profile running the extension.");
    }
  }

  function modelName(option) {
    const title = findVisible(option, '[data-test-id*="title"], [class*="title"]');
    return (title?.innerText || option.innerText || label(option))
      .split("\n")
      .map(line => line.trim())
      .find(line => line && !/^(lock|lock_outline|check|check_circle|radio_button_checked)$/i.test(line));
  }

  function locked(option) {
    if (!enabled(option) || /\b(locked|upgrade|requires .* plan)\b/i.test(label(option))) return true;
    return !!findVisible(option, 'mat-icon, [data-mat-icon-name], [data-icon], [aria-label]', el =>
      /^(lock|lock_outline|locked)$/i.test(
        el.getAttribute("data-mat-icon-name") || el.getAttribute("data-icon") || label(el)
      )
    );
  }

  function selectableModels(menu) {
    const choices = new Map();
    for (const option of menu.querySelectorAll(CHOICES)) {
      if (!visible(option) || locked(option)) continue;
      const name = modelName(option);
      if (name && !/^(close|cancel|back|sign in|manage plan|thinking level)$/i.test(name)) {
        choices.set(modelId(name), option);
      }
    }
    return choices;
  }

  async function modelMenu(picker) {
    const openMenus = new Set(Array.from(document.querySelectorAll(MENUS)).filter(visible));
    picker.click();
    return waitFor(() => {
      const id = picker.getAttribute("aria-controls");
      const controlled = id && document.getElementById(id);
      if (controlled && visible(controlled)) return controlled;
      return findVisible(document, MENUS, menu => !openMenus.has(menu));
    }, "Gemini's model menu did not open.");
  }

  function selectedModel(picker) {
    const current = picker.getAttribute("aria-label")?.match(/\bcurrently\s+(.+)$/i)?.[1];
    return modelId((current || modelName(picker) || "").trim());
  }

  async function selectModel(picker, requested) {
    if (selectedModel(picker) === requested) return;
    const menu = await modelMenu(picker);
    const option = selectableModels(menu).get(requested);
    if (!option) {
      throw new GeminiError("The selected model is unavailable for this Gemini account. Refresh the model list.");
    }
    option.click();
    await waitFor(
      () => {
        const current = findVisible(document, PICKER);
        return current && selectedModel(current) === requested;
      },
      "Gemini did not confirm the selected model. Refresh the model list."
    );
  }

  function textWithBreaks(node) {
    if (node.nodeName === "BR") return "\n";
    if (node.nodeType === Node.TEXT_NODE) return node.data;
    return Array.from(node.childNodes, textWithBreaks).join("");
  }

  function composerText(composer) {
    // The editor can render an empty line as a paragraph containing one break.
    const lines = [];
    let inline = "";
    for (const node of composer.childNodes) {
      if (node.nodeName === "DIV" || node.nodeName === "P") {
        if (inline) lines.push(inline);
        inline = "";
        const blank = node.childNodes.length === 1 && node.firstChild.nodeName === "BR";
        lines.push(blank ? "" : textWithBreaks(node));
      } else {
        inline += textWithBreaks(node);
      }
    }
    if (inline) lines.push(inline);
    return lines.join("\n");
  }

  async function insertPrompt(prompt) {
    const normalized = prompt.replace(/\r\n?/g, "\n");
    const message = "Gemini's chat composer did not accept the complete prompt.";
    for (let attempt = 0; attempt < 2; attempt++) {
      const composer = await waitFor(() => findVisible(document, EDITOR), message);
      composer.focus();
      const range = document.createRange();
      range.selectNodeContents(composer);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      const whitespace = composer.style.whiteSpace;
      composer.style.whiteSpace = "pre-wrap";
      try {
        document.execCommand("insertText", false, normalized);
      } finally {
        composer.style.whiteSpace = whitespace;
      }
      await waitFor(
        () => !composer.isConnected || composerText(composer).trimEnd() === normalized.trimEnd(),
        message,
        2000
      );
      if (composer.isConnected) return;
    }
    throw new GeminiError(message);
  }

  async function openChat(request) {
    requireAccount();
    await waitFor(
      () => findVisible(document, EDITOR),
      "Gemini's chat composer is unavailable. Open Gemini in Chrome and check your sign-in."
    );
    requireAccount();
    const picker = await waitFor(
      () => findVisible(document, PICKER),
      "Gemini's model picker is unavailable in this Chrome profile."
    );
    if (request.operation === "models") {
      const menu = await modelMenu(picker);
      const models = [...selectableModels(menu).keys()];
      if (!models.length) throw new GeminiError("Gemini returned no selectable models.");
      return {models};
    }

    await selectModel(picker, request.model);
    await insertPrompt(request.prompt);
    const send = await waitFor(
      () => findVisible(document, "button", button => enabled(button) && /^Send message$/i.test(label(button))),
      "Gemini's Send message button is unavailable."
    );
    const prior = new Set(document.querySelectorAll("model-response"));
    send.click();
    const answer = await waitFor(() => {
      const response = findVisible(document, "model-response", element => !prior.has(element));
      if (!response || !findVisible(response, "button", button => enabled(button) && /^Copy$/i.test(label(button)))) {
        return undefined;
      }
      return findVisible(response, "message-content", element => element.innerText.trim());
    }, "Gemini did not finish the response. Check the site for usage limits or errors.",
    request.connection_test ? 23000 : 120000);

    const copy = answer.cloneNode(true);
    for (const element of copy.querySelectorAll("button, [role=button], script, style, [aria-hidden=true]")) {
      element.remove();
    }
    return {html: copy.innerHTML};
  }

  chrome.runtime.onMessage.addListener((request, sender, reply) => {
    if (sender.id !== chrome.runtime.id || sender.tab
        || !["models", "submit"].includes(request.operation)) return false;
    openChat(request).then(reply, error => reply({
      error: error instanceof GeminiError ? error.message : "Gemini's chat interface could not be read.",
    }));
    return true;
  });
})();
