# Browser languages

The shared browser interface supports English, Simplified Chinese, Hindi, Spanish,
Arabic, French, Bengali and Portuguese. Display settings lists the native names.
On first visit, the interface uses the first supported browser language; a saved
choice on this device takes precedence. Unsupported saved choices fall back to
English. Regional language tags such as `es-MX` and `ar-EG` select the base locale.

Navigation, settings, search, wallet and certificate labels and primary post
controls use the shared catalog. Some auxiliary operation/status messages still
fall back to English. User posts, channel descriptions, command output, API names
and protocol documents are not automatically translated. The CLI/TUI locale
support is separate from the browser catalog.

Arabic switches the interface to right-to-left; switching back restores
left-to-right. Code and the MSG brand retain left-to-right reading order. Locale
selection does not change authorization or translate identifiers sent to APIs.

Add a locale to `LANGUAGES` and a complete column in `browser_i18n.py`; catalogs
are embedded in the existing CSP-hashed script without another network request.
Run the browser language, rendering and OAuth integrity tests after changes.
