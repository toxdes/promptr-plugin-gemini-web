# Gemini Web provider

Gemini Web supports your signed-in Gemini account in Chrome and a guest session
in Chromium. Both return Markdown when Gemini finishes. Each request opens a
new Gemini chat. Follow-ups include Promptr's saved history and agent
instructions in the prompt.

Install `uv` and make it available on [Promptr](https://github.com/toxdes/promptr)'s
`PATH`. Clone this repository into
`${XDG_CONFIG_HOME:-$HOME/.config}/promptr/plugins/gemini-web`:

```sh
git clone git@github.com:toxdes/promptr-plugin-gemini-web.git \
  "${XDG_CONFIG_HOME:-$HOME/.config}/promptr/plugins/gemini-web"
```

Replace `promptr` with `promptr-debug` in the destination for a debug build.
Run the setup commands below from the installed directory.

## Use your Gemini account

1. Sign in to Gemini in the Chrome profile that has your Google AI plan.
2. In the same profile, open `chrome://extensions` and enable **Developer mode**.
3. Click **Load unpacked** and select this provider's `extension` directory.
4. Copy the **Promptr Gemini Web** extension ID. From the provider directory,
   register the native messaging host.

   ```sh
   ./native_host.py --register EXTENSION_ID
   ```

   The command registers Google Chrome on Linux by default. For Chromium or
   Chrome for Testing, pass `--browser chromium` or
   `--browser google-chrome-for-testing`.

   If Chrome still points to an earlier checkout, remove that host registration
   and run the command again with the extension ID from the new location.

5. Click **Promptr Gemini Web** in Chrome's extensions menu. Its badge shows
   **ON** when the native host is connected.
6. In Promptr, open **Edit → Preferences**, choose **Add provider**, and select
   **gemini-web**. Set **Browser session** to **signed-in**.
7. Click **Refresh available models**, select a model, and run **Test connection**.
   Save the profile after the test passes.

Keep Chrome open while you use the provider. Promptr checks the selected model
before sending each prompt. Gemini applies the history and usage settings of
your signed-in account. The extension and native host run locally.

## Use guest mode

1. From the provider directory, install the guest browser.

   ```sh
   ./gemini_web.py --install-browser
   ```

2. Create a **gemini-web** profile with **Browser session** set to **guest**.
3. Run **Test connection** and save the profile. Guest mode uses Auto.
