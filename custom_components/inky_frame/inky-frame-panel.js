/*
 * Sidebar panel for the Inky Frame integration.
 *
 * It embeds the gallery the frame serves itself (GET /gallery) rather than
 * rebuilding a picker out of Lovelace cards: the gallery already knows the library,
 * uploads, the prefs and the panel's live state, and it works unchanged on a phone
 * outside Home Assistant.
 *
 * A custom panel is handed the whole viewport and gets NO Home Assistant toolbar —
 * unlike a Webpage dashboard, which is drawn inside one. So the header here is not
 * decoration: without its menu button there is no way back to the sidebar on a phone,
 * where the sidebar is collapsed. `hass-toggle-menu` is the event the frontend's own
 * toolbar fires, and it reaches Home Assistant because the panel is registered with
 * embed_iframe=false — our element lives in the HA document, so a composed event
 * bubbles out of the shadow root to it.
 *
 * The URL has to be reachable BY THE BROWSER, not by Home Assistant — and it must be
 * https:// whenever Home Assistant is, or the browser blocks the frame as mixed
 * content. That is why it is a setting on the config entry instead of being derived
 * from the address the integration talks to.
 */
class InkyFramePanel extends HTMLElement {
  // Home Assistant assigns these on every state update. Only `panel` carries anything
  // this element renders from, and re-rendering on the others would reload the iframe
  // constantly.
  set hass(value) { this._hass = value; }
  set narrow(value) { this._narrow = value; }
  set route(value) { this._route = value; }

  set panel(panel) {
    const url = (panel && panel.config && panel.config.url) || "";
    const title = (panel && panel.title) || "Inky Frame";
    if (url === this._url && title === this._title && this.shadowRoot) return;
    this._url = url;
    this._title = title;
    this._render();
  }

  connectedCallback() {
    if (!this.shadowRoot) this._render();
  }

  _toggleMenu() {
    this.dispatchEvent(
      new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })
    );
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const body = this._url
      ? `<iframe src="${this._url}" allow="clipboard-write"></iframe>`
      : `<div class="empty">
           No gallery URL set yet. Open <b>Settings → Devices &amp; services →
           Inky Frame → Configure</b> and set the address a browser can reach the frame
           on — use the <code>https://</code> one if Home Assistant is served over
           HTTPS, or the browser will block this page as mixed content.
         </div>`;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: flex; flex-direction: column; height: 100%; }
        header {
          display: flex; align-items: center; flex: none; box-sizing: border-box;
          height: var(--header-height, 56px); padding: 0 4px;
          background: var(--app-header-background-color, var(--primary-color, #03a9f4));
          color: var(--app-header-text-color, var(--text-primary-color, #fff));
          font-family: var(--paper-font-body1_-_font-family, sans-serif);
        }
        button {
          background: none; border: 0; color: inherit; width: 40px; height: 40px;
          border-radius: 50%; display: grid; place-items: center; cursor: pointer;
        }
        button:hover { background: rgba(255, 255, 255, .12); }
        svg { width: 24px; height: 24px; }
        .title { font-size: 20px; margin-left: 12px; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
        iframe { border: 0; flex: 1; width: 100%; min-height: 0; }
        .empty { padding: 2rem; line-height: 1.5;
                 font-family: var(--paper-font-body1_-_font-family, sans-serif);
                 color: var(--secondary-text-color, #666); }
        code { background: var(--secondary-background-color, #eee);
               padding: .1em .3em; border-radius: 4px; }
      </style>
      <header>
        <button id="menu" title="Open the sidebar" aria-label="Open the sidebar">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" stroke-width="2"
                  fill="none" stroke-linecap="round"/>
          </svg>
        </button>
        <div class="title">${this._title || "Inky Frame"}</div>
      </header>
      ${body}`;
    this.shadowRoot.getElementById("menu").onclick = () => this._toggleMenu();
  }
}

// The module URL is versioned by the file's mtime, so a page that outlives an update
// can end up importing two copies. Defining the same name twice throws, which would
// take the panel down for a reason nobody could see.
if (!customElements.get("inky-frame-panel")) {
  customElements.define("inky-frame-panel", InkyFramePanel);
}
