/* Opens the LDAP Browser page.
 *
 * - Toolbar button: opens a new LDAP Browser tab (each tab is its own session).
 * - Address bar: type `ldap dc01.corp.example` or `ldap ldaps://dc01:636`
 *   to open the browser with that server filled in.
 */

const PAGE = browser.runtime.getURL("browser.html");

async function openBrowser(target, disposition = "currentTab") {
  const url = target ? `${PAGE}#${encodeURIComponent(target)}` : PAGE;
  if (disposition === "currentTab") {
    await browser.tabs.update({ url });
  } else {
    await browser.tabs.create({ url, active: disposition === "newForegroundTab" });
  }
}

browser.action.onClicked.addListener(() => openBrowser(null, "newForegroundTab"));

browser.omnibox.setDefaultSuggestion({
  description: "Open LDAP Browser for a server (e.g. dc01.corp.example or ldaps://dc01:636)",
});

browser.omnibox.onInputEntered.addListener((text, disposition) => {
  openBrowser(text.trim() || null, disposition);
});
