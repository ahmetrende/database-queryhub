# What's-new screenshots

The What's-new entries show these images. `GET /api/changelog/media/<file>`
serves them to the web page and to the portal tab. `CHANGELOG.md` and pull
request bodies show the same files.

- Use mock data only: the prototype in `QueryHubWeb/QueryHub.html` or a
  local stack with fixtures. Never take a screenshot of a real system.
- Name the files `<entry-id>-before.png` and `<entry-id>-after.png`, in
  lower case.
- A changed screen gets a before and an after image. A new screen gets an
  after image only. An invisible change gets no image.
- Crop to the part of the screen that changed. Keep a file under 400 KB.
