# Imported Gemini Web compatibility layer

The `gemini_web2api` package, historical standalone entry point,
account-pool utilities, browser cookie extension and regression tests were
imported from the user's working deployment on 2026-10-17, based on
https://github.com/Sophomoresty/gemini-web2api commit
`2bb988b` plus the user's local fixes. This snapshot includes image upload,
thought passthrough, model presets and account-pool fixes.

The imported code retains its MIT notice in
`licenses/gemini-web2api-MIT.txt`. The destination repository's GPLv3
`LICENSE` is preserved. No credentials, account data, real configuration,
runtime logs, backups, or browser state were imported.

The legacy Gemini Web transport uses an undocumented website protocol.
Compatibility does not imply upstream support or verified identity of the
website's model aliases. New providers must report their actual transport.
