# VI Clips YouTube Session Bridge

Install the unpacked extension from `chrome://extensions` or `edge://extensions`
using Developer mode and Load unpacked. Reload VI Clips afterward. In the import
dialog, enable **Use my YouTube browser session** once. Imports and retries then
refresh the session automatically without a cookies.txt export or repeated prompt.

This installation grants access only to YouTube cookies. The bridge runs only for
the top-level VI Clips page at `http://localhost:3166` or `http://127.0.0.1:3166`.
It checks the exact app origin even though Chrome's content match pattern covers
other localhost ports. It reads no cookies for Google or other websites, skips
partitioned cookies, stores no cookie values, and never sends values in page messages.

The isolated content script uploads to the fixed same-origin API route with redirects
disabled. The existing private SSH tunnel carries the request to OCI, where the
session is encrypted at rest. Video download and clip processing remain on OCI.

Use **Disconnect and remove saved session** in VI Clips to disable sharing in this
browser and remove the saved server session. Remove this extension to revoke its
browser permission. Previously queued connected jobs cannot use a deleted session
and will need reconnection. Session sharing cannot guarantee YouTube accepts OCI's IP.
