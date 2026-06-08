# Download header expectations

What RetroShelf sends for each format, and how to verify with `curl -I`. These are
the headers old Mobile Safari (iOS 5.1.1–12) needs for the iBooks hand-off.

## EPUB

```
curl -I "http://localhost:8099/download/<id>/Some-Book.epub"
# Expect:
# HTTP/1.1 200 OK
# Content-Type: application/epub+zip
# Content-Disposition: attachment; filename="Some-Book.epub"
# X-Content-Type-Options: nosniff
# Cache-Control: no-store
# Accept-Ranges: bytes
```

- `application/epub+zip` is what makes Safari offer **Open in iBooks** (Safari can't
  render EPUB inline). A wrong type causes the `.epub.zip` / `.zip` failure.
- The URL path also ends in `.epub` — old WebKit names the saved file from the URL
  path, so the extension is correct even though Safari may ignore the CD `filename`.

## PDF

```
curl -I "http://localhost:8099/download/<id>/Some-Book.pdf"
# Expect:
# HTTP/1.1 200 OK
# Content-Type: application/pdf
# Content-Disposition: inline; filename="Some-Book.pdf"
# X-Content-Type-Options: nosniff
# Cache-Control: no-store
```

- `inline` lets Safari render the PDF; the user then taps **Share → Copy/Open in Books**.
- Set `PDF_DISPOSITION=attachment` to force a hand-off instead of inline render.

## Range / 206

When the client sends `Range: bytes=...`, RetroShelf forwards it to Kavita and relays
`206 Partial Content` + `Content-Range` when Kavita supports it; otherwise it streams
the full `200`.

## What you must NEVER see

- `Content-Type: application/octet-stream` or `application/zip` for an EPUB.
- The Kavita `apiKey` anywhere in a URL, link, `<img src>`, or response body.
- `Content-Disposition` without a `filename`.
