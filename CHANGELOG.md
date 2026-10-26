# Changelog

All notable changes to QueryHub are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). What the major number does and does not
cover is spelled out under "Versioning and support" in the README —
`bot_config` keys and the audit contract are inside the promise; the
frontend and the endpoints it calls are explicitly outside it.

## [Unreleased]

### Added

- **The audit screen shows the channel of a request.** Each request row has a small pill
  with the door the request came through: Slack, Web, IdP or MCP. The side panel has a
  "Channel" line. `POST /api/admin/audit/search` adds `origin` (the stored value) and
  `channel` (the name to show) to the `request` block of each row. An unknown value
  shows as itself. The search box also finds a channel: type `idp` to see the requests
  from the IdP.
- **The side panel of the audit screen shows the decision.** The `request` block adds
  `submittedAt`, `decidedAt`, `decidedBy`, `decidedById` and `decisionNote`. The panel
  shows when the person sent the request, who decided it and when, and the note. The name comes
  from the people list, and the name stored on the request is the fallback. An undecided
  request shows none of these lines.

## [1.0.38] — 2026-10-25

The results grid sorts, filters and totals in the page, and a result downloads as Excel CSV, JSON, Markdown or SQL INSERT. The editor formats SQL. A person can withdraw an auto-approve window request, and a request shows its steps. An approver can approve RO or RW, never DDL. The release adds migrations 195 and 196. Patch bump.

### Added

- **Sort, filter, sum and JSON view in the results grid.** Each column header has a sort
  button. It cycles ascending, descending and the original order. A filter box above the
  grid keeps the rows that contain the text. Write `column:value` to search one column.
  Both work on results of up to 5,000 rows. A larger result shows the filter box off,
  with the reason as its tooltip. A dragged column width stays while you sort. When you
  select two or more cells, a bar shows the count, sum, average, minimum and maximum. A
  JSON cell opens as indented text in the full value view, and a Raw button shows the
  original.
- **The Export menu offers the new formats.** It has four more items: CSV for Excel, JSON,
  Markdown table and SQL INSERT. They call the `result.*` routes that the entry below
  describes. A refusal, such as a result that is too large, shows its message in a toast
  and saves no file. The menu opens upward when the window has little room below it.
- **Format SQL in the editor.** Shift+Alt+F lays out the selected text, or the whole text
  when the selection is empty. It uses the `sql-formatter` package with the dialect of the
  connection's engine. It changes spaces and line breaks only, and keeps case, quotes,
  casts and comments. A Format button next to the word wrap button does the same. One
  Undo restores the text and the selection. The raw prototype has no formatter, so the
  key does nothing there and the button is absent.
- **More ways to download a result.** `GET /api/queries/{id}/result.json`, `result.md` and
  `result.sql` return the stored, masked result as JSON, a Markdown table or INSERT
  statements for a placeholder table name. A result with more rows than
  `result_export_max_rows` (default 100000, migration 196) answers 413. `result.csv?bom=1` adds a UTF-8
  byte order mark, so that Excel shows Turkish letters right. The stored file does not
  change. Add `?statement=N` to pick one table of a multi-statement result.
- **The audit screen says when one rule does most of the auto-approving.** `GET
  /api/admin/audit/insight` looks at the queries that QueryHub approved by itself
  in the last 30 days and groups them by rule and by the tier of the query: RO,
  or RW for writes. For a tier with at least 20 such queries, where one rule
  produced at least half of them, the answer names that rule, its count and its
  share. Otherwise the list is empty. The audit screen shows the answer in a
  callout with a "Review rule" button that opens the Auto-approve screen. The
  route uses the same gate as the audit search.
- **Access shows when it ends.** `GET /api/connections` adds `grantEndsAt` to each
  database entry: an ISO time, or null for no end. The value is the end of the
  access the person holds at the highest tier there. When two grants give the same
  tier, the one that ends last counts. A grant with no end makes it null. The
  Welcome lists show "44d left" or "Ends soon · 47h left" on each row. The tree
  shows an amber "Ends in 47h" chip when the end is within seven days.
- **A request shows its steps.** `GET /api/queries/{id}` adds `timeline`: the steps
  sent, decided, started and finished, with the time of each. Only the steps that
  happened appear. A decided step carries the outcome and the name of the person
  who decided. The Messages tab shows them in a "Steps" list. The list endpoints
  do not carry it.
- **A person can withdraw an auto-approve window request.** `POST
  /api/auto-approve-requests/{id}/withdraw` withdraws a request that nobody has
  decided. A request of another person answers 404 `not_found`. A request that is
  decided answers 409 `already_decided`. Migration 195 adds the status `withdrawn`.
  The request dialog has a Withdraw button. A DBA decision on a withdrawn request
  answers 409 "The person withdrew this request."
- **A grant revoke can carry a reason.** `DELETE /api/admin/grants/{gid}` and
  `DELETE /api/admin/grants/fleet/{gid}` accept an optional body `{"reason": ...}`
  with at most 500 characters. The reason goes into the audit row and nowhere
  else. A request with no body still works. Both revoke rows have an optional
  "Reason" field.
- **Three outbox events for SQL requests.** `queryhub.request_decided`,
  `queryhub.request_withdrawn` and `queryhub.request_expired` go to the requester.
  The setting `idp_outbox_enabled` (default off) gates them, as it gates the
  other events. `docs/AUTH.md` section 1.3 lists the payload keys.

### Changed

- **Destructive admin actions ask inline.** Removing a team, an owner, a role, a
  mask exemption or a connection, disabling a connection, and pausing or resuming
  an auto-approve rule now open a row with the same sentence and two buttons. The
  browser dialog is gone. The kill switch history shows a name in "By" and in
  "released by" when the people list knows the id.
- **An approver can approve RO or RW, never DDL.** A DBA scope (`POST` and `PUT
  /api/admin/scopes`) with DDL as its highest tier answers HTTP 400 with the code
  `tier_not_allowed`. So does a role `approver` (`POST` and `PATCH
  /api/admin/roles`) with a DDL ceiling or with no ceiling, because no ceiling
  means every tier. The scope form shows RO and RW. The role form offers RO and
  RW and has no "No ceiling". A super-admin and a role `admin` are not
  affected. The database has no constraint, so a row that SQL writes still works.
  Rows that exist keep working: none holds DDL on the live system today, and a
  stored row with DDL opens in the form with Save off until you pick RO or RW.
  Code: `src/queryhub/approver_limits.py`. `docs/OPERATIONS.md` section 32.

## [1.0.37] — 2026-10-24

The web editor shows where a request stands and what ran. The admin screens work from the keyboard and show the state of the kill switch and of failing connections. A new DBA level opens three admin pages. The kill switch stops approvals as well as runs. Auto-approve rules and grants from access requests now end, and a rule is never DDL. Connections report their health. The release adds migrations 145 to 150, 160 and 190 to 194. Patch bump.

### Added

- **The kill switch stops approvals too.** While it is on, every approval that
  runs a request is refused with "Kill switch is on. Resume queries first." This
  covers the web decision, the web batch approve, the web batch decision, the
  Slack Approve button, "Approve all remaining" and the approval of a CSV import.
  The web answers HTTP 409 with the code `kill_switch_on`. A deny and a request for
  changes still work. The queue turns its approve buttons off.
- **A grant from an access-request approval ends in 90 days.** The new setting
  `access_grant_default_days` (default 90, from 1 to 365) sets the default end.
  `POST /api/admin/endpoint-requests/{id}/decision` accepts `expiresAt` for another
  end and `noEnd: true` for no end. The Slack Approve button takes the default.
  `GET /api/admin/endpoint-requests` returns `accessDefaultDays`. Grants that exist
  keep their end dates. The Provision card has an "Access ends" picker.
- **A `dba` admin level.** A person with a permanent admin role that covers every
  connection passes it. A tier ceiling does not block it, and a super-admin passes. A
  temporary, scoped or approver-only role does not. `GET /api/me` returns
  `admin.isDba`. Five routes need this level and no longer need `access`:
  `POST /api/admin/kill`, the read of `GET /api/admin/people/{id}/effective-access`,
  `GET /api/admin/connections`, `POST /api/admin/connections/{conn}/test` and
  `GET /api/admin/people`. The Slack `/sql kill` command follows the same rule. A
  row for a `dba` caller has no `credentials`, `secretsProvider` or `notes`, and a
  failed test hides the user name and the password. A person row for a `dba`
  caller has `id` and `name` only, so the portal's person picker has names to
  offer. A super-admin gets the whole row.
- **Kill switch history, and a cancel of running reads.** `GET /api/admin/kill/history`
  lists the pauses, newest first. Setting the kill switch on cancels the read-only
  queries that run. A write and a schema change keep running, because a cancel
  can leave a half-applied change. The app sets the switch first, then cancels the
  reads. `POST /api/admin/kill` returns `cancelledCount`.
- **Periodic connection health.** `scripts/check_connection_health.py` checks each
  target when it is due. Each row of `GET /api/admin/connections` carries `health`
  (`ok`, `checkedAt`, `failingSince`, `error`, or `null`) and `inRotation`. The
  settings are `connection_health_enabled`, `connection_health_interval_sec`,
  `connection_health_timeout_sec` and `connection_health_exclude_engines`. A
  ClickHouse check is a query, so it keeps a cloud service awake: the last
  setting is `clickhouse` by default (migration 192), so ClickHouse is skipped.
  Empty the setting to check it.
- **Your own access requests.** `GET /api/endpoint-requests` lists them.
  `POST /api/endpoint-requests/{id}/withdraw` withdraws one that nobody has
  decided. The new status is `withdrawn`.
- **`outcome` and `expiresAt` on a request.** `GET /api/queries/{id}`, the queue
  items and `GET /api/history` carry `outcome` (`withdrawn`, `expired`, `denied`,
  `changes_requested` or `null`). `GET /api/queries/{id}` and the queue items also
  carry `expiresAt`, which is `null` while request expiry is off. `GET
  /api/queries/{id}/result` returns `tier`.
- **One decision for a bundle.** `POST /api/admin/queue/bundles/{id}/decision` takes
  `{decision, note}` and answers `{bundleId, decision, items}`. A 409 lists the
  items that already have a decision. An item that QueryHub approved at submit does
  not take part.
- **Extend a grant, pause a rule, end a scope, expire an exemption.**
  `PATCH /api/admin/grants/{gid}` takes `expiresAt` and `reason`. An ended grant
  answers 409. `PATCH /api/admin/auto-grants/{id}` takes `enabled`, and the list
  adds `enabled` and `runCount`. Admin scopes, fleet grants and mask exemptions
  take `expiresAt`. A mask exemption also takes `audience` and returns
  `audienceTeamId`. Every reader of an expiry or of `enabled` enforces it.
- **Metric windows and a last-query time.** `GET /api/admin/metrics` takes
  `?window=7d|30d` and adds `window`, `windowStart` and `decisionTime`.
  `GET /api/admin/people` adds `lastQueryAt`. A draft does not count.
- **RW auto-approve requests, off by default.** The setting
  `auto_approve_rw_requests` turns them on. `GET /api/auto-approve-requests`
  returns the new `tiers` field.
- **The audit log shows failed requests.** Rows that say a request stopped with
  an error are no longer hidden. The screen has a "Failed requests" filter with
  a count. A failed row says "Failed" and shows the error text in the list. The
  rail opens with the error and a Copy button. The search endpoint takes
  `outcomes: ["failed"]`, and each row carries `outcome`, `request.status` and
  `request.error`. The hidden "per-request lifecycle" slice no longer holds
  `failed`.
- **Four outbox events for the IdP portal.** QueryHub writes
  `queryhub.access_request_pending`, `queryhub.access_request_decided`,
  `queryhub.window_request_pending` and `queryhub.window_request_decided` to
  `notification_outbox`, next to the Slack DMs. The rows serve the time when
  Slack is off. `docs/AUTH.md` §1.3 lists their payload keys. The setting
  `idp_outbox_enabled` (default off) gates every row. Add a template for each
  event in the portal before you turn the setting on.
- **The IdP portal can tell a person why QueryHub refused them.** A refused
  identity assertion now names its cause in `error.code` when the cause is the
  person: `not_onboarded` (no account), `bad_domain` (an address outside
  `web_allowed_email_domain`) or `ambiguous_address` (two accounts share the
  address). Every other refusal keeps the generic body.
- **`GET /api/admin/people/duplicate-addresses`.** A super-admin can list the
  addresses that two different people share, with the rows that hold them. Such
  an address blocks a sign-in. The endpoint changes nothing.
- **A switch that closes the web sign-in to everyone but super-admins.** Set
  `web_own_ui_super_admin_only` to `on`. A person who is not a super-admin can
  then no longer sign in with Slack, SSO or a local account. Their open sessions
  end at once. A signed portal request and Slack are not affected. The sign-in
  screen shows "Refused: QueryHub opens through the portal. Sign in there." The
  default is `off`. Migration 145 adds the key.

- **The connection list shows the cloud and the environment on every row.** Each
  server row has the provider logo and a PROD or STG chip. PROD is a solid orange
  pill. Chips read at 11 px, and only PROD, STG, RO, RW and DDL stay in capitals.
  The confirm-before-run dialog shows the statement in a code box with the target
  chip. The request-access picker shows the provider logo and the environment, and
  it locks Read/write and Schema change on read-only engines, with the reason. The
  auto-approve request dialog now warns about an ask that already waits, and it
  shows "Ends on <date>". Dialogs no longer open with a heavy focus ring.
- **Words match the portal.** "Approve and run", "Deny", "Withdraw", "Submit for
  DBA approval", "Read/write" and "Schema change" replace "Approve & run",
  "Reject", "Cancel request", "Submit for approval", "Read/Write" and "Schema
  (DDL)" on the web. Server values, API fields and Slack texts do not change.
- **The provider logo follows the theme.** The dark logo variant loads on first
  paint in dark mode.
- **The target strip shows the cloud and the engine.** Next to Run, the strip reads
  the provider logo, the environment tag, the engine logo, then the connection and
  the database. A connection with no provider tag shows no provider logo.
- **A click on a database uses it in a blank tab.** A new blank tab starts with a
  default database. The first click on a database in the tree sets the tab to it,
  and the tab then keeps it, so browsing the tree never changes a connection. To
  change it later, double-click the database for a new query or right-click it.
  A tab with SQL in it is never changed by a click. A blank tab says this on its
  target strip, and each database row says it in its tooltip.
- **The Kill switch screen lists past pauses.** Each row shows who paused, when,
  why, how long it lasted and how many reads it cancelled. After you turn the
  switch on, the screen says how many running reads stopped. It also says that
  writes and schema changes keep running.
- **A DBA opens three admin pages.** The pages are Kill switch (with its button),
  Effective access and Connections. Connections is read-only for a DBA. Every
  other page shows as a locked row with a lock icon and the words "Super-admin
  only", not as a missing item. A deep link to a locked page opens the queue.
  The screens read `admin.isDba` of `GET /api/me`. A server that does not send
  it gives an admin the old pages only.
- **A reject note needs 15 characters.** Reject and Request changes show a
  counter and four chips: "Try on test first", "Add a WHERE clause", "Link an
  incident" and "Inside change freeze". A chip adds a ready sentence. The
  server still accepts any note, so a note from Slack is not refused.
- **Extend a grant, pause an auto-approve rule, end an admin scope.** The Grants
  screen has an Extend button on a grant with an end date, and an "Ends soon"
  chip within seven days. A rule has Pause and Resume, a "Paused" chip and a
  run count. A paused rule cannot be edited, because an edit would resume it. A
  scope form has an end date, and a scope row says "Permanent" or its end.
- **Masking exemptions show who they unmask for and when they end.** The form has
  an audience (everyone, admins only, super-admins only or one team) and an end
  date. A row with the `dba` or team audience no longer reads "everyone".
- **Connections show a health chip, and People show the last query.** The chip
  says "Healthy", "Failing since ..." with the error, or "Not checked". A
  replica says "In rotation" or "Out of rotation". A "Failing" filter lists the
  failing connections. The People list says when a person last sent a query.

- **A request shows where it stands.** A request that waits for a DBA shows a pale
  "Waiting for a DBA" pill and a Withdraw button in the action bar. The results
  area shows an amber card with the time since the submit. The History sidebar
  has a "Waiting" group. Denied, Withdrawn, Expired and "Changes requested" each
  have their own pill, and the history list has a filter for them. A request with
  changes requested has an "Edit and resubmit" button. It opens a new tab with the
  same SQL and target.
- **The Welcome screen says what runs at once.** Two lists, "Runs at once" and
  "Goes to a DBA first", come from the connections the person holds. Each list
  shows six rows at most, then "+N more". A super-admin sees one line.
- **The bell opens what it names.** A click on an item opens the request, or the
  admin page for an admin item. The bell menu has a "Browser notifications" switch,
  off by default. When it is on, each new item raises one browser notification.
- **The Messages tab says what ran.** A finished request reads "184 rows in 412
  ms." When the run masked columns, a second line reads "1 PII column masked:
  iban." It names columns and never a value. A run on a read replica adds "Ran on
  <name>, about 2.3 s behind." Only a super-admin sees the replica name. A request
  without a row count keeps the old line.
- **The environment chip is smaller.** The chip is 10 px with weight 600. Colours
  and contrast do not change (5.00 in light, 7.41 in dark).
- **The approval queue opens the first request.** The detail pane has a "View
  access" button. It opens the Effective access screen for the requester
  (`#admin/effective?person=<id>`). A "Requester holds" line shows the tier and the
  database, and an "Expires in N min" line shows when the request has `expiresAt`.
  The keys J and K move between cards. A, D and C approve, deny and ask for
  changes. A field or a dialog with focus turns the keys off.
- **The Kill switch screen shows its state.** A banner reads "Queries are running"
  or "Queries are paused", with who and why. "Pause all queries" is a solid red
  button. "Resume queries" is calm. The admin menu shows a second line "Ask a
  super-admin" on a locked row. Connections has a red badge with the count of
  failing connections.
- **The Metrics screen shows decision time.** The screen has "Last 7 days" and
  "Last 30 days" tabs (30 days by default). A card shows the median and the p90
  time to a decision, the bands, and short lists by server and by team (five rows
  each).
- **Connections show a banner for failing connections.** The red banner names up
  to three connections and the oldest failing time. "Test again" re-tests only the
  failing connections. The provider has its own column. A DBA still sees the list
  read-only.
- **Revoking a grant asks first.** A row under the grant says "<person> loses <tier>
  on <database>. A query that is running finishes." The buttons are "Keep access"
  and "Revoke access". The fleet grants still use the browser confirm.
- **The admin scope form picks people and connections.** The admin comes from the
  person picker and the connections are chips. The saved payload does not change.
- **Masking exemptions have an "Ends soon" filter.** It keeps the rows that end
  within seven days and shows a count. The chips use sentence case.

- **Removing a grant on every server asks first, and "No end date" is one word.** The
  Remove button opens an inline row that says who loses read-only access and that a
  running query finishes. The end pickers and the rows say "No end date" where they
  said "No expiry". The masking screen uses the same seven-day line as the other
  screens for "Ends soon".

### Changed

- **The image lock moves to `certifi` 2026.10.23 and `setuptools` 84.2.1.** The
  release audit found known advisories in the two older pins. No other pin changes.
- **An auto-approve rule needs an end date, and is never DDL.** A rule that the
  app creates has an end, at most `auto_approve_max_days` days from now (new
  setting, default 90, from 1 to 365; migration 193). A request with no end, or
  with an end that is too far away, answers HTTP 400 with the code `end_required`
  or `end_too_far`. A rule for tier DDL answers 400 with `tier_not_allowed`: a
  rule is RO or RW. This covers the Auto-approve screen (`POST
  /api/admin/auto-grants` and `/bulk`), a grant with auto-approve
  (`POST /api/admin/grants`), a grant on every server with auto-approve, copy
  access and the approval of a window request. Copy access gives a copied rule
  the end of its source, at most the limit, and skips a DDL rule. The forms hide
  "No expiry", stop the picks at the limit and start on 30 days.
  `GET /api/admin/auto-grants` returns `maxDays`. The Slack `/sql grant` box for
  auto-approve is refused, because that modal has no end date field. The
  database enforces nothing: a DBA can still insert a rule with no end with SQL
  (OPERATIONS.md §30), and every reader treats it as permanent. Rules that exist
  keep their end dates.
- **An RW auto-approve request answers 403 while `auto_approve_rw_requests` is
  off.** That is the default. Before, a person who held RW could ask for one.
  Nobody did. The web form greys out "Reads and writes" and says why. Production
  keeps one more rule: only a super-admin decides an RW window.
- **The kill switch cancels running reads.** See "Kill switch history" above.
- **The Home cards show a loading state.** History, saved queries, saved
  sessions and scheduled runs load after sign-in. Until they arrive, each card
  shows grey placeholder rows with no count, not "0" and "Your recent queries
  show here". The placeholder ends after a failed load too, and when the lists
  are empty for real the old text and "0" appear.
- **The web app compresses text responses with gzip.** The JSON of
  `/api/connections` was about 2 MB for a reader with many connections, and the
  JS bundle 750 KB. Both went over the network as they were, so the server list
  stayed empty for seconds on a slow link. The app now packs JSON, script, CSS,
  HTML, SVG and CSV, which makes the connections answer about ten times smaller.
  Spreadsheets, images, fonts and event streams are not packed again. A client
  that does not send `Accept-Encoding: gzip` gets the plain body.
- **The server list shows a loading state.** Before the first `/api/connections`
  answer, the sidebar shows grey placeholder rows with the label "Loading
  servers", not an empty tree that reads as "no servers". The placeholder stops
  after a failed load too, and it respects reduced motion.
- **The audit log list has a Details column.** Each row has columns for time,
  kind, action, target, details, who and request. Details is one line: the
  error, the SQL or the main facts of the event. The empty space in the middle of
  a row is gone. The column names stay in view while you scroll. The up and down
  arrow keys move through rows, and Escape closes the rail. A narrow list drops
  Details, but a failed row keeps its error on a second line.
- **The access-request and window-request routes of the portal no longer need
  `slack_app`.** They work when Slack is off, and also when Python cannot import
  the package. The window lengths and their labels moved to
  `window_options.py`. Each route imports `slack_app` only in the branch that
  has a Slack client. The Slack messages do not change.
- **The connection lists show "SQL Server" and "Amazon Athena".** They showed the
  raw ids `mssql` and `athena`. The label table now has one entry for each of
  the four engines that the database accepts. The engine icons did not change.

### Fixed

- **QueryHub is the approver of a super-admin's own query.** The Messages tab
  already said so. The stored record now says it too. The request names QueryHub
  as the approver, and the audit row names the super-admin whose access applied.
  The audit screen files these approvals under the auto-approver, not under the
  person. Older records stay as they were, and they still read as QueryHub in
  the tabs. Slack status lines say "Auto-approved by QueryHub". The auto-approve
  rate in Metrics now counts these queries.
- **The Messages tab names the request and its real level.** The first line now
  reads "Submitted DDL query as request #12180". It used to read "Submitted RO
  query" for a script that opened with a DO block. The tab worked out the level
  from the text. It now uses the level that the submit stored. The History list
  does the same.
- **"Edit access" shows real access only.** The editor used to open a team without
  grants on a pre-filled row. The row held the first connection, RO and all
  databases, and Save wrote it. A team without grants now opens on "No access
  yet", and a new row starts empty. A search box picks the connection. Each field
  has a label, and one bar says what Save changes. A removed connection stays in
  place until Save, with Undo.
- **A statement with no result says so, and Messages opens.** The
  Results panel used to centre a big "0" over "0 row(s) affected". It now says
  "No result" at the top left, with a second line that fits the statement. A read
  says it returned no rows. A write says it affected 0 rows. A write that changed
  rows says how many, for example "5 rows affected". A schema change says
  "Statement executed". The run now finishes on the Messages tab, because Results
  has no grid to show.
- **A query that QueryHub approves by itself says so.** The Messages tab used to
  read "Submitted RO query for DBA approval". Then it named the requester as the
  approver. No DBA saw that query. It now reads "Submitted RO query" and
  "Auto-approved by QueryHub", and it gives the reason. The reason is super-admin
  access, an auto-approve grant, or the same query as an earlier request. The
  Audit log tab and the history list name the system as the approver too. The
  stored decision does not change.
- **A read replica leaves the schema catalog.** The hourly refresh skips a read
  replica, because it serves its primary's reads under the primary's name. A
  target that became a replica after its last snapshot kept its rows, and
  nothing removed them. Search could show them. The hourly refresh now removes
  the catalog rows of every read replica and writes one `schema_catalog_pruned`
  audit row for each. A run that names a target or a database does not do this.
  Run `python3 scripts/refresh_schema_catalog.py --prune-report` for a dry run.
- **A database that refuses the read-only login leaves the schema browser.** The
  hourly refresh listed every database on a server and then failed to open the
  ones that refuse the read-only login. Each one failed once an hour, and its old
  tables stayed in the sidebar. The refresh now lists only the databases that the
  login may open, and it logs the ones it leaves out. The prune removes the old
  rows of such a database, as it does for a dropped one. The admin schema refresh
  answers 404 for it, and the message says why.
- **A database that is gone leaves the schema browser.** The hourly refresh
  snapshots the databases that exist and never removed the rows of one that was
  dropped, so it stayed in the sidebar with its tables. The refresh now removes
  the catalog rows of a database that the server no longer lists, and writes one
  `schema_catalog_pruned` audit row. It removes nothing when the listing fails or
  comes back empty, when the plan would empty the target, or on a run that names
  one database. It acts on PostgreSQL and SQL Server only. Run
  `python3 scripts/refresh_schema_catalog.py --prune-report` for a dry run.
- **A name dropped on the editor goes to the caret.** You drag a database or a
  table from the sidebar into the editor. The name used to land where the pointer
  let go, often at the start of the line. It now lands at the caret, as a paste
  does, and it replaces a selection. Undo takes it back in one step.
- **A SELECT that calls `setval` or `nextval` now needs the write tier.** Such a
  statement changes a sequence, but it ran as a read with the read-only login.
  The server refused it with "Permission denied for sequence". Where the
  read-only login holds sequence privileges, it would have used up values with
  no write review. `currval` and `lastval` stay reads. The rule applies to
  PostgreSQL only.

## [1.0.36] — 2026-10-14

Cancel now stops a running write or schema change. An error message no longer
shows a value that the database read from a row. A sweep on real servers closed
the phrasings that the first version missed. Every web response carries an
`X-Request-ID`. A state change and its audit row commit together. Both services
warn about pending migrations. The `mssql` CI job tests SQL Server live and found
three defects. This release fixes one. Two stay open. Patch bump.

### Upgrading

- **Apply the migrations before you restart, as before.** From now on, the web
  process answers `/readyz` with 503 while a migration file is missing from the
  ledger. Both services log a CRITICAL line at start. This release adds no
  migration.
- **Restart every QueryHub process.** The cancel path, the audit writes and the
  web app changed.
- **The text log format of the web process changed.** Each line now has `[id]`
  before the message, and `[-]` outside a request. Update a log parser that
  matches the old format. A JSON line gets a `trace_id` field inside a request.

### Added

- **Every web response carries an `X-Request-ID`.** The server makes the id and
  ignores one that the client sends. An unhandled 500 also holds it as
  `error.traceId`. Each log line of a web request carries it as `trace_id` in
  JSON, or as `[id]` in text. A user report that quotes the id leads to its log
  lines.
- **Startup guards.** Both services log a CRITICAL line when migrations are
  pending. The web app logs one when its base URL is `http://` and the host is
  not loopback. `/readyz` answers 503 while migrations are pending. A check
  stays silent when it cannot know: no migrations directory, no ledger, or a
  login that cannot read the ledger.
- **Live SQL Server tests in CI.** The `mssql` job runs
  `tests/test_integration_mssql.py` against a throwaway SQL Server 2022 and fails
  if the tests skip. It found three defects. The Fixed section below covers the first. The
  other two are open and written in `docs/KNOWN_LIMITATIONS.md`: the login of
  `deploy/grant_mssql.sql` has no `SHOWPLAN`, so a script of several writes
  commits only its last statement, and Stop cannot reach a running SQL Server
  query.
- **A test fails when a setting has no documentation.**
  `tests/test_config_docs_sync.py` checks every `bot_config` key and every
  environment variable that the code reads, and every local link in the docs. The
  reference now lists six keys and six variables that it missed.

### Changed

- **SQL Server errors show the message itself.** The driver tuple and the
  SQLSTATE prefix no longer appear in front of it.

### Fixed

- **Cancel stops a running write or DDL request.** The signal used the RO login,
  and PostgreSQL refuses it for a backend that runs as another login. The
  request ran until `query_timeout_sec`. The signal now uses the login of the
  tier that the request runs at, and never a higher one. The runaway watchdog
  stops long writes too. Each cancel opens one more session for that login, so a
  login at its connection limit makes the cancel fail.
- **A state change and its audit row commit together.** A password change, an
  IdP sync, the cancel of a scheduled query and the decline of a window request
  were two commits. A failure between them left a change with no audit row, or
  half of an IdP sync. Each now runs in one transaction. `tests/test_audit_discipline.py`
  fails on a new standalone audit row for a state change.
- **`deploy/grant_mssql.sql` no longer denies `CONTROL SERVER`.** That denial
  also denied `CONNECT SQL`, so its logins could not log in. The script now
  revokes it from a login that an older version made.

### Security

- **An error message no longer shows a value that the database read from a
  row.** `SELECT full_name::int FROM customers WHERE id = 1` failed with that
  name in the message, and the result file would have masked it. While PII
  masking is on, `[hidden]` replaces the value. A value that the requester typed
  in the SQL stays visible, and a CSV import shows every value. The list covers
  PostgreSQL, SQL Server, ClickHouse and Athena. Masking still does not stop
  inference with a predicate and a row count (`docs/COMPLIANCE.md`, section 4).
- **The error scrubber has a time limit.** The content detectors took 38 seconds
  on 200 KB of one character, so a large cell could block an executor thread
  while it was cast. `scrub()` reads at most 8000 characters in the hiding
  step and 1000 in the content detectors.
- **A sweep on real servers closed the phrasings that the first version
  missed.** It ran 642 failing statements on PostgreSQL 17 and SQL Server 2022,
  with a sentinel value in every column type. Of them, 187 still showed the
  value: 138 of 398 on PostgreSQL and 49 of 244 on SQL Server. A cast to
  `regclass` or a call of `current_setting()` with a column puts a value where a
  name goes, so a name that is not in the requester's SQL is hidden too. A typo
  that the requester wrote stays visible. All the cases now pass.
  `docs/COMPLIANCE.md` lists what stays: the free text of `RAISE`, `ASSERT`,
  `THROW` and `RAISERROR`.

## [1.0.35] — 2026-10-13

QueryHub refuses a mistake in a write or a script when you submit it, and the
pre-flight can no longer run a hidden second statement. An admin sets the owner
teams of a connection, gives a person or a team read access on every server,
and marks a server super-admin-only. A decision on the web retires the Slack
cards. The DM poller survives a database outage. Patch bump.

### Upgrading

- **Run `scripts/apply_migrations.py`.** It applies 141–143. Apply 141 before
  you restart, because the new code reads `target_servers.super_admin_only`.
- **Restart every QueryHub process.** Each one runs its own copy of the
  executor and the pre-flight.
- **A new key, `pre_flight_validate_scripts`,** has the default `on`
  (migration 143). Set it to `off` to go back to the old check. It needs no
  restart.

### Added

- **QueryHub refuses a mistake in a write or in a script when you submit it.**
  It plans every statement and runs none. A wrong column name, a
  missing table or a syntax error that PostgreSQL rejects now stops the
  request before an approver sees it. The message names the statement.
  Set `pre_flight_validate_scripts` to `off` to go back to the old check.
- **An admin sets the owner teams of a connection.** `GET/POST/DELETE
  /admin/connections/{conn}/owners` manage the hand-made owners. QueryHub
  refuses a synced owner and names the sync. The lead of each
  owner team approves requests to that connection from any requester, up to
  the fleet's ceiling (RO today). Each change writes an audit row
  (`target_owner_added`, `target_owner_removed`) and runs the lead-approver
  reconcile for that one target. The Connections screen has an Owners dialog,
  and the picker names each team's lead.
- **A person or a team can get read access on every server, and a server can
  be super-admin-only** (migration 141).
  - `GET/POST/DELETE /admin/grants/fleet` manage one `all_targets` row per
    subject, RO only, with an optional fleet-wide waiver in the same
    transaction. A write needs a super-admin and `access_model_v2`. QueryHub
    refuses a mirrored row and names its source.
  - `PUT /admin/connections/{conn}/super-admin-only` marks a server. On a
    marked server, fleet-wide grants, fleet-wide waivers and the admin shortcut
    count for no one but a super-admin. A grant or a waiver that names the
    server still counts. Only a super-admin can write one.
- **An access request can be approved on the web.** The Connections screen
  lists pending requests with the tier that Provision grants. A decision there
  retires the admin cards in Slack and tells the requester.
- **A super-admin can add a reason to a run that needs no approval.** The
  web shows "Add a reason" where no reason is asked for. The audit log keeps
  the reason with the request.

### Changed

- **User-facing messages follow Simplified Technical English.** Slack, the
  web API and the MCP server give the cause first, then the fix. Only the
  words changed. No check decides differently.
- **A column named `tier_name` is no longer masked as a person name.** The
  `name` rule now skips a column name that has the word `tier`. Other name
  columns stay masked.
- **The masking Edit box shows its three settings at once.** Replace with the
  same reach saves on the original row. A real replace writes the new row,
  then disables the old one. A refused duplicate says how to change its
  settings.
- **The access-request card has no SQL recipe.** Approve writes the per-user
  grant, so the admin does not run SQL first. The card keeps the attempted
  query of the requester.
- **The Scheduled panel says where a run goes.** `GET /scheduled` sends
  `runOn` and `replicaName`.
- **The screens use Simplified Technical English.** The admin screens,
  sign-in, home and panels carry design's copy. Masking says Disable and
  Enable.
- **Athena connections show the AWS Architecture icon.**
  `THIRD_PARTY_NOTICES.md` names Amazon as the owner of the mark.

### Fixed

- **A comment in front of a write no longer hides its row estimate.** A
  request that started with `-- ...` or `/* ... */` got no "~N rows affected"
  line, because the check read the comment as the first word. QueryHub also
  refused an `EXPLAIN` with a comment in front as a syntax error. Both work now.
- **Approving again after a lapse restores access.** The approval kept the old
  end date. An active grant of the same tier keeps the end date it has.
- **The DM poller survives a database outage.** The loop read its interval in
  the `while` condition, outside the `try`. One outage ended the thread, and
  no DM went out until a restart. A failed read now keeps the last good
  interval, and the interval is at least one second.

### Security

- **The pre-flight `EXPLAIN` cannot run a hidden second statement.** QueryHub
  now sends it with the extended protocol, and PostgreSQL itself refuses a
  text that holds more than one statement. A tail such as `ROLLBACK; DELETE ...`
  could leave the read-only transaction when the statement split and the
  server disagreed. Measured on PostgreSQL 18: the tail deleted the row. Now the
  server refuses it, and QueryHub logs a warning.

## [1.0.34] — 2026-10-05

The web editor undoes every edit, expands `*` to exactly the columns a
statement reads, and quotes reserved words. For a table that the catalog does
not list, such as a partition, QueryHub reads its columns live from the
target. A super-admin can choose where one query runs, a grant can carry
auto-approve, and a deleted instance shows as deleted. The trusted-portal seam
is ready, and a sync that would disable many people waits for a super-admin.
Patch bump.

### Upgrading

- **Run `scripts/apply_migrations.py`.** It applies 132–140.
- **`WEB_SESSION_SECRET` must be at least 32 bytes when you set it,** or the
  web process does not start. If you do not set it, QueryHub derives the key
  from the master key, as before.
- **An unreadable `secrets.enc` stops the process.** It used to read the
  plaintext environment instead. `QH_SECRETS_PLAINTEXT_FALLBACK=1` allows that
  on purpose.
- **New `bot_config` keys:** `web_slack_team_id`, `target_ssl_verify_hosts`,
  `target_ssl_verify_exempt_hosts`, `web_employment_grace_hours`,
  `idp_clock_skew_seconds`, `idp_outbox_enabled`, `idp_outbox_retention_days`,
  `idp_sync_max_disable` and `athena_freshness_stale_hours`. See
  docs/CONFIGURATION.md.
- **The `*` expansion reads a relation's columns live** through
  `GET /connections/{conn}/databases/{db}/columns`, with the RO login
  (PostgreSQL only). It needs no new privilege: every login can read
  `pg_attribute`.

### Added

- **A grant can carry auto-approve, in the same transaction.** The web grant
  form has an Auto-approve box with a tier (RO by default, never above the
  grant's tier, never DDL). `/sql grant` has an "Auto-approve read-only
  queries" box. `POST /admin/grants` takes `autoApprove` / `autoApproveTier`.
  - QueryHub writes one waiver per granted database, with the grant's expiry.
    If a waiver fails to write, QueryHub reverts the grant.
  - QueryHub does not write a waiver when the subject already holds an equal
    or broader one. The response (`autoApprove`), the toast and the Slack
    summary name the held waiver that covers the grant.
  - A person's waiver goes to `auto_approve_grants` (the migration-109 mirror
    projects it). A team's waiver is an `access_grant` row in the grant's
    transaction.

- **A super-admin can choose where one query runs:** Auto (as before), the
  primary, or a named read replica (`runOn` on `POST /queries`, migration 137).
  See OPERATIONS.md §26.
  - A chosen replica skips the automatic rules. If it cannot run the query,
    the request fails and says why. The request never runs on the primary
    instead.
  - QueryHub audits each honoured choice (`execution_run_on_forced`). The
    status poll and history rows say where a query ran (`ranOn`).
  - A scheduled request keeps the choice, and now keeps `unmasked` too. The
    scheduler used to drop it, so a scheduled unmasked result arrived masked.
- **An Athena approver sees how far the archive reaches.** The hint adds
  "Archive complete up to <date> UTC", with any known gaps, or "Archive coverage
  unknown". It comes from the marker that the archive writes
  (`engine_config.freshness_marker`). See docs/CONFIGURATION.md.
  - The hourly catalog refresh reads it, for enabled and disabled targets, and
    stores the verdict in `target_servers.archive_freshness` (migration 140).
  - If the archive does not rewrite the marker within
    `athena_freshness_stale_hours` (36), the hint adds a warning.
  - If QueryHub cannot read the whole marker, the hint says unknown, never
    complete.

### Changed

- **QueryHub offers auto-approve only where the subject can already query.**
  A waiver skips review. It grants no access. A waiver written anywhere else
  did nothing until the subject got access there. On that day, the access
  arrived already approved.
  - The Auto-approve form lists nothing until you pick a person or team. Then
    it lists only the connections and databases they reach, with the tier
    capped at what they hold there.
  - `POST /admin/auto-grants` and `/auto-grants/bulk` refuse a target that the
    subject cannot query, and a tier above the one the subject holds. They
    answer `409`, name what they refuse and write nothing. Each refusal carries
    `connectionId` / `databaseId`, so the form marks the row that caused it.
  - A fleet-wide row reads "every server they can reach", not "all databases".

- **A target whose instance was deleted shows as deleted, and no one can
  enable it.** It looked like any other disabled target, and the admin screen
  offered to enable it. Measured 2026-09-30: 26 of 68 disabled targets were
  deleted instances. Every one had an endpoint that no longer resolved.
  - Migration 139 adds `target_servers.deleted_at` / `deleted_reason`, with a
    CHECK that forbids a target that is both deleted and enabled. So no path
    can enable a deleted target.
  - The hourly inventory sync marks any target whose instance is gone, enabled
    or not. If the endpoint returns, the sync clears the mark but does not
    enable the target.
  - The admin connection list shows deleted targets last, under their own
    heading, with no controls. A request to enable one gets `409 deleted`.
  - Fixed: an instance recreated under the same name gives its endpoint both a
    deleted and a live inventory row. QueryHub judged such an endpoint by
    whichever row came last, so it could disable a live target as deleted. The
    live row now wins.

- **A pod lead approves requests on their pod's servers from anyone.** The
  lead's approver role used to require the request to come from the lead's own
  pod as well. So a person with access to another pod's database reached no
  approver except the admins. This applied to 58 of 120 single-server personal
  grants, and to 28 of 268 requests decided in 30 days.
  `scripts/sync_team_approvers.py` now writes each owned-server row for any
  requester (`all_teams`). The ceiling (RO by default) and the rule that no one
  approves their own request stay the same.

- **The trusted-portal seam is ready for its first connection** (AUTH.md §1.2).
  Everything here is inert while `idp_assertion_enabled` is off.
  - QueryHub accepts an assertion with up to `idp_clock_skew_seconds` (10) of
    clock skew. It refuses an assertion when no email domain is configured or
    when the assertion lives longer than 120 s. It also refuses any assertion
    on a websocket.
  - The reconcile has a dry run. It never disables an admin, a role holder or
    the sync account.
  - The reconcile and the outbox answer only the sync principal. That
    principal needs no admin role.
  - QueryHub writes the notification outbox only while `idp_outbox_enabled` is
    on. The daily cleanup trims it.
- **A portal sync that would disable more than five requesters waits for a
  super-admin.** The reconcile disables everyone who is not on the panel's
  list. So a wrong list would block that many people from Slack, the web and
  MCP within one tick. For example, the list is wrong when a role is not
  granted yet. A run over the limit now changes nothing and answers
  `409 approval_required`. Every super-admin gets one Slack card that asks
  whether they approve.
  - Approve covers those people, once, for 24 hours. Reject keeps the list
    blocked until it changes.
  - The limit is `idp_sync_max_disable` (default 5, migration 138). All of this
    is inert while `idp_assertion_enabled` is off.
- **The S3 metrics dashboard matches the admin panel's Metrics view:** the
  same typeface, cards, section labels and colours. The build inlines the
  typeface, so the page stays one file. As in the panel, RW is blue and
  rejected is amber. Status mixes are stacked bars, and the per-team, top-user
  and admin-workload charts are the panel's labelled bars. The filters and
  every figure stay the same.
- **A super-admin can end a session, run a mixed script and set
  `search_path`.** `pg_terminate_backend` / `pg_cancel_backend` ask first and
  run as the elevated role. A script that mixes tiers runs as one request at
  its highest tier. `SET search_path` takes a list of schema names for the one
  request. QueryHub still refuses all three for everyone else. See
  OPERATIONS.md §25.
- **ClickHouse `system` and `information_schema` tables are readable.** A
  requester with a grant on a ClickHouse target can query its partition sizes,
  columns, running merges and query log. Before, QueryHub refused them as
  server internals. Table functions stay default-deny.
- **QueryHub records a statement that the web refuses** in
  `submission_failures`, as it already did for the Slack modal's refusals. The
  record holds the SQL (with passwords masked) and the reason. QueryHub does
  not record a confirmation prompt.

- **The MCP `describe_database` search picks its tables in SQL.** A loose
  filter used to read every column of every matching table and keep 25. On the
  largest catalogue here, that read 31,412 rows in 124 ms. The search now reads
  353 rows in 4 ms.
- **An Excel download of a CSV result needs only one conversion.** QueryHub
  keeps the workbook beside the result and deletes it with the result.
- **The container image shows its build.** The build stamp was empty in the
  image, which has no `.git`. The release stamps the commit and version, and
  `QH_IMAGE_DIGEST` adds the image digest.

- **Every admin screen stops at 1600 px on a wide monitor,** centred beside
  the nav. The approval queue's detail pane stops at 1120 px. This replaces the
  Audit log's own 1480 px cap from 1.0.33. Nothing changes below about 1860 px
  wide.
- **The admin panel is one click away at the head of the left column,** with
  the approval badge on it. The Developer / Admin tabs left the top bar. When
  the sidebar is hidden, the entry becomes an icon beside its toggle.
- **The admin panel opens in your real role.** The "Viewing as" switch is gone.
- **Only a super-admin sees the kill switch's button.** Other admins still see
  its state, who engaged it and why. The server already refused them.
- **"Browse connections" on the Welcome page works.** It opens the sidebar if
  the sidebar is hidden, shows Connections and puts the cursor in its search
  box.
- **Back from the admin panel returns to the editor.** The panel's first
  history write replaced the editor's entry.
- **The admin Connections screen lists each read replica under its primary.**
  It says whether the replica is in rotation. It offers no credential rotation
  for the replica, because the replica uses its primary's login. Every other
  admin picker omits replicas.
- **The admin Connections table fits its panel.** Test and Edit stay on each
  row. The actions to rotate credentials, disable, refresh the schema and
  delete moved into a ⋯ menu. Engine and hosting share one column, and the
  environment tag sits beside the name. The pinned actions column no longer
  covers the databases. In a narrow window, the table scrolls sideways and
  does not squeeze the database names.
- **The masking exemption form's pickers filter as you type.** Server,
  database, schema, table and column still accept only what the catalog lists.
  A typo keeps the previous choice. The column picker clears after each pick.
- **The effective-access screen describes the per-server rule.** A team's
  grant names each member whose own grant on that server applies instead. The
  summaries say "auto-approved".

### Fixed

- **When the editor writes a column or table named like a reserved word, it
  now quotes the name.** `*` expansion, autocomplete and tree drags wrote
  `user`, `order` or `group` bare, and the query failed. For `user`, the query
  returned the login's name on every row instead (PostgreSQL and SQL Server
  both read it as the current user). `qhQuoteIdentFor` now quotes each
  engine's reserved words: PostgreSQL's key-word table, Trino's for Athena, and
  Transact-SQL's reserved keywords. ClickHouse and MySQL use the PostgreSQL
  list plus their clause words. Nothing changes for Oracle, because Oracle
  folds names to upper case.

- **`*` in the web editor expands to the columns of what the statement reads,
  and nothing else.** On a partition, Tab on the star filled the select list
  with every column in the database.
  - Cause: the catalog merges partitions into their parent. For a table that
    the catalog did not list, the star used the whole database's columns. The
    star also took its table from the first `from` anywhere in the editor, not
    from the statement under the caret.
  - Fix: the editor reads the table for each star from the star's own
    statement. For a table that the catalog does not list, the editor reads
    the columns live through
    `GET /connections/{conn}/databases/{db}/columns?table=`. That endpoint
    runs a fixed read-only metadata query (`to_regclass` + `pg_attribute`)
    with the RO login and a 3 s timeout, on PostgreSQL only. If the target
    does not know the table either, the editor offers nothing.
  - A join expands the columns of every table, each qualified by its table's
    alias. `t.*` expands t.

- **You can undo every edit in the web editor.** ⌘Z / Ctrl+Z did nothing after
  an edit that the editor made itself. These edits are `*` expansion with Tab,
  an accepted completion, Tab indent, a dropped tree object, and a whole-line
  cut or paste.
  - Cause: those edits set the controlled textarea's value, and that empties
    the browser's own undo stack.
  - Fix: the editor keeps its own history, one per tab. It owns ⌘Z / Ctrl+Z,
    ⌘⇧Z / Ctrl+Shift+Z and Ctrl+Y (not on a Mac). Typed text makes one undo
    step per word. Every other edit is one step. That includes text that the
    app inserts from outside.
  - The history survives a tab switch, and sign-out clears it.
- **On an Athena archive, only admins, the owning team's lead and waiver
  holders skip review, and only for reads.** A fingerprint match would have run
  anyone's repeat query there with no review. When `engine_config.auto_approve`
  is not set (docs/CONFIGURATION.md):
  - The lead of the team that owns the archive, and anyone with an admin role,
    auto-approve reads, with or without a waiver. The request records
    `archive: owner lead` or `archive: admin` as the reason.
  - A waiver that covers the read applies, whether it is fleet-wide or names
    the archive.
  - Everyone else's query goes to that lead and the admins. The fingerprint
    cache never applies, and QueryHub offers no auto-approve window.
  - No screen shows more than RO there to anyone except a super-admin.
    QueryHub refuses every write there, as before.
  - `true` makes the target behave like any other. `false` lets no query skip
    review except a super-admin's own query, which is unchanged.
- **"All databases" in the web grant forms wrote a grant on a database named
  `*`.** It matched nothing. Any spelling of every database (`*`, empty, `all`,
  `any`) now means every database, for person and team grants alike.
- **A team's auto-approve rows show as the team's.** The list recognised a
  team by a `(team)` suffix that only the prototype's mock sends. So on the
  real server, a team's card read as a person. "Add targets" posted the team's
  name as a person id, and the server refused it.
- **A statement too big to check gets a refusal with a message, not a 500.**
  sqlparse stops parsing a statement of more than 10,000 tokens, which is
  about 3,000 values in an `IN (...)` list. It also stops on a statement nested
  more than 100 levels deep. The refusal says how to split the statement, or
  how to pass a long list as one value.
- **QueryHub refuses a long chain of operators in milliseconds.** Each new
  operator in `a + b + c ...` made sqlparse 0.5 re-read the whole chain. So
  1,000 operators took 7 s to refuse and held a web worker the whole time.
  sqlparse 0.6 builds a group's text from its children's text, and QueryHub
  now requires it (`sqlparse>=0.6.0`, as the image's lock already had).

- **The metrics dashboards read pods, not the emptied teams table.** Every
  request showed as "(unteamed)" after pods replaced teams. The per-team chart
  and filter now use the requester's current pod (migration 135).
- **Who-can-what, `/sql whoami` and `/sql roles` read the access model.** They
  listed legacy grants and missed roles and grants written after the switch.
- **A request's tier on the dashboard is the tier it ran at.** A leading
  comment or a line break after SELECT made the dashboard count a read as DDL.
- **Approval latency counts decisions taken by people.** Auto-approvals are
  most of the decisions, and they pulled every percentile to near zero. The S3
  dashboard shows their count on its own card and excludes them from admin
  workload. A pod captain's decisions show the captain's name instead of a
  Slack id.

- **`/api/auth/local/start` answers 404, not 500.** The password provider has
  no redirect leg, and the redirect routes called it anyway.
- **An AWS secret that is JSON but not an object fails with a clear error.**
  A string, a list or `null` passed the parse, and QueryHub cached it for the
  TTL. It failed later, as an `AttributeError`.

- **An all-RO batch reaches the pod captain, as a single RO request does.**
  The batch DM went to admins alone, and its bulk buttons were admin-only. A
  scoped approver now gets the batch when they can approve every item. The
  bulk buttons admit the same people.
- **A person's own grant replaces their team's on the whole server,
  everywhere.** The database list already worked this way. Submit, the
  effective-access screen and team auto-approve decided it per database. So
  the screen could show a team grant that the person could not pick. To keep
  the team's grants as well, set `merge_with_team` on the person's own grant.
- **Two browser tabs that refresh at the same time no longer end your
  session.** The second refresh got a different token from the first. Then the
  tab whose token the browser kept could not refresh again. A rotation now
  always issues the same successor for the same token.
- **An admin with no requester row keeps their name after a refresh.** The
  refresh read the profile from the requesters table only.

### Security

- **You can split the metadata database's roles, so the service cannot edit
  the audit trail.** `scripts/split_metadata_roles.py` moves every object to a
  NOLOGIN owner. After the split, the runtime login has DML on tables, SELECT
  on views and only SELECT/INSERT on `audit_log`. Migrations then run as a
  migrator (`BOT_DB_MIGRATOR_USER`, `BOT_DB_MIGRATOR_PASSWORD`,
  `BOT_DB_OWNER_ROLE`), which re-applies the runtime's grants after each run.
  The script has plan, rehearse, apply and rollback modes, and the runbook is
  docs/OPERATIONS.md §28. CI runs the integration suite a second time as a
  split runtime.
- **The container image installs a hash-locked dependency set.**
  `docker/requirements.lock` pins every package that the image installs, with
  the hashes of its files. The build backend is in it too. Before, two builds
  of one commit could ship different wheels, and nothing checked what the
  build downloaded. Regenerate the lock with `scripts/lock_image_deps.sh`. A
  release now stops on a known vulnerability in that set.
- **A web write needs a live Slack check of the person.** QueryHub refuses an
  RW/DDL submit with a 503 when `users.info` gives no answer, and nobody loses
  their session. A sign-in or refresh with no answer passes only if Slack
  called the person active within `web_employment_grace_hours` (default 2). A
  transport error used to pass everyone. The write check ran for Slack sign-ins
  only, and now covers SSO sessions too. users.info's `user_not_found` counts
  as gone (migration 134).
- **An unreadable `secrets.enc` stops the process.** It used to read the
  plaintext environment instead. `QH_SECRETS_PLAINTEXT_FALLBACK=1` allows that
  on purpose.
- **A `WEB_SESSION_SECRET` shorter than 32 bytes stops the web process.**
  If you do not set it, QueryHub derives the key from the master key, as
  before.
- **QueryHub can check target certificates for one host, or one cloud, at a
  time.** Nothing changes until you set a key (migration 133).
  - `target_ssl_verify_hosts` switches the listed hosts to `verify-full`
    against `target_ssl_rootcert`.
  - `target_ssl_verify_exempt_hosts` is for a server that cannot pass the
    check. It keeps that server on `require`.
  - SQL Server follows the same lists. Two connections that hardcoded
    `require` (the CSV import's table check and the web roles list) follow the
    settings too.
  - The CA file now goes only with a mode that checks the certificate, because
    libpq reads `require` plus a root file as `verify-ca`.
  - The metadata DB takes `BOT_DB_SSLMODE` and `BOT_DB_SSLROOTCERT`.
- **QueryHub now guards the header row of an export against formulas too.** It
  already guarded the data cells. A column alias such as `"=HYPERLINK(...)"`
  opened as a live formula in CSV and XLSX downloads, including the web's
  CSV-to-XLSX conversion.
- **QueryHub refuses a Slack sign-in when it cannot check the workspace.** It
  used to skip the workspace comparison whenever `auth.test` failed. The new
  key `web_slack_team_id` pins the workspace, for an install that runs Slack
  sign-in without the bot (migration 132).
- **The avatar proxy checks every redirect.** `urlopen` follows redirects on
  its own, so the host allow-list checked only the first URL. The proxy now
  checks each hop against the allow-list and follows at most three.
- **An email resolves to one principal across both people tables, or to
  none.** If a row in `requesters` and a different person's row in `admins`
  shared an address, the address resolved to whichever table QueryHub asked
  first. Every external sign-in and the principal sync now use one resolver
  (`requesters.principal_by_email`).
- **QueryHub keys the AWS Secrets Manager cache by region as well as secret
  id.** The same secret name in two regions returned the first region's
  credentials for both.

## [1.0.33] — 2026-09-23

Two engines execute: ClickHouse and Amazon Athena, both read-only. A read-only
query can run on a healthy read replica. A submitted password is no longer
stored anywhere. Patch bump: every new `bot_config` key keeps the old behaviour
by default, and the audit contract is unchanged. New actions are added, and no
existing row changes meaning.

### Upgrading

- **Run `scripts/apply_migrations.py`.** It applies 126–131. 129 changes how a
  password in submitted SQL is stored. 130 corrects 129's trigger, which named
  a status the enum does not have, and so failed every write to `requests`. The
  runner applies both together; do not stop between them.
- **Optional extras.** `pip install '.[clickhouse]'` for ClickHouse targets.
  Athena uses `'.[aws]'` (boto3). Neither is needed if you run neither engine.
- **New `bot_config` keys**, each defaulting to what happened before:
  `replica_routing` (`off`), `replica_max_lag_seconds`,
  `replica_health_ttl_seconds`, `replica_read_your_writes_minutes`, and
  `<engine>_blocked_functions`. See docs/CONFIGURATION.md.
- **SQL copied back out of history may be refused.** A statement stored with
  `PASSWORD '***REDACTED***'` is refused until the password is typed again.

### Security

- **A submitted password is never stored.** `requests.query` is written with
  every password literal masked. The original is kept Fernet-encrypted in
  `requests.query_secret` only while the request can still run, and dropped
  when it ends, including when it is handed to a DBA. Saved workspaces,
  templates, favourites and refused submissions keep the masked text. So do
  Slack messages, snippets, upload comments and log lines.
- **ClickHouse runs read-only by construction.** The login is `readonly=1`,
  table functions are refused unless allowed, `SETTINGS` clauses are refused,
  and `system.*` and `information_schema` are blocked. An operator can refuse
  more functions with `clickhouse_blocked_functions`, and cannot allow any.
- **An Athena approver sees what a query can scan before approving it.** The
  bound is read from the objects the query can touch, not from catalog
  statistics, which were measured five weeks stale.

### Added

- **ClickHouse executes**, over the native protocol (9440, TLS). The read-only
  login cannot set server-side limits, so a watchdog enforces the timeout and
  a cancel by closing the connection, which stops the query on the server.
  The schema catalog is read at most daily, and only when the inventory says
  the service is running: an idle service is billed when a query wakes it.
  Import with `import_targets_from_inventory.py --clickhouse`.
- **Amazon Athena executes**: Glue-backed schema, result values kept as the
  exact strings Athena returns, and the bytes scanned recorded in the audit
  trail.
- **Read replicas.** A read-only PostgreSQL request runs on a healthy, enabled
  replica of its target, and on the primary otherwise. Replicas are linked
  from the inventory (`target_servers.replica_of`, migration 131) and never
  appear in a picker. Some reads stay on the primary: those about the server
  itself (`pg_stat_*`, `pg_locks`, WAL functions), and a requester's reads
  just after their own write there. A replica that fails a read hands it to
  the primary once. The result says it came from a replica, and
  `requests.executed_target_id` records which one. Off until
  `replica_routing = 'on'`; see docs/OPERATIONS.md §26.
- **Admin:** enable, disable or set a credential on many connections at once,
  all or nothing. Edit a role in place. See one team's effective access, and
  a person's, resolved per database. Add auto-approve for a team or for many
  targets in one step. A queue lists requests waiting for a DBA to run by
  hand, and closes them from the web.
- **Requesters can ask for auto-approve from the web**, and an admin decides
  there or in Slack.
- **Imported targets are labelled with the cloud they run on** (`tags.provider`).

### Changed

- **A new connection can take the name a disabled one holds.** The disabled
  one takes its engine's suffix (`orders` → `orders-pg`). An enabled holder
  keeps its name: the importers suffix the newcomer, and the admin form
  refuses the name.
- **"Ask to skip review" is "Request auto-approve"**, and every screen says
  auto-approve for the same idea.
- **A team's auto-approve applies to its members when they submit**, as the
  screens already showed.
- **A connection test shows green or red.** A connection an admin enables or
  adds appears in the list without a page reload.
- **The audit log stops at 1480 px** on a wide screen, so the actor and the
  request id stay next to the action they belong to.

### Fixed

- **A request handed to a DBA no longer shows as running forever** on the
  web. It says "Needs a DBA", with no spinner and no Stop button.
- **The RDS import step no longer stops on a name clash.**
- **The ClickHouse catalog** compares the inventory's naive timestamps in the
  inventory's own time zone, and stores its flags as booleans.

## [1.0.32] — 2026-09-17

The panel names people instead of logins, the masking ladder's second rung is
held to its schema, and two screens that were broken only in the built bundle
work again. Patch bump: the `bot_config` keys and the audit contract are
unchanged.

### Security

- **A table-level masking exemption is held to its schema.** The ladder's table
  rung compared BARE table names, so a row written for one schema's table also
  covered a same-named table in every other schema of that database — the fault
  the column rung had, on the louder rung: this one returns `skip_all`, masking
  off for the ENTIRE result rather than one column. The rung now runs through
  the same `_schema_admits` check, which can only ever REFUSE: a row with no
  schema, a statement that writes the table unqualified, and SQL that will not
  parse are all admitted exactly as before. Measured the way the column rung
  was — the resolver replayed over every completed request that recorded its
  result columns, 3,545 requests and 911 invocations, with and without the
  schema on table rows: no answer changed. **1.0.31's notes described this fix
  a release early; the code is here.**

### Added

- **Masking exemptions can be edited from the panel.** `PATCH
  /admin/mask-exemptions/{id}` now carries the reason, the strength, the join
  behaviour and the audience, not just `enabled`. **Reach stays immutable** — a
  row's reason describes that row's reach, so a row whose reach moved carries a
  reason that is a lie about production data. Narrowing is still
  turn-off-and-create. Scope fields are DECLARED and REFUSED (`400
  reach_immutable`, naming which ones) rather than dropped, because a client
  that believes it just narrowed a row, against a server that ignored the
  field, shows an operator a protection they do not have. Migration 125 adds
  `updated_by` / `updated_at`.
- **A target whose schema catalog stops refreshing says so.** The hourly
  refresh logged a warning and returned the same either way, so a target could
  serve a stale snapshot for days — an expired credential, a host that moved —
  and the first symptom was somebody asking why a new table is missing from the
  picker. Migration 124 adds `schema_refresh_health`, one row per target,
  overwritten each run. A target counts as failed only when NOTHING was
  snapshotted.

### Changed

- **People are shown by name, not by login.** A person reached the API as three
  different strings — a Slack id, a `first.last` login, and an ascii-folded
  display name — and every screen printed whichever one it got. They are now
  resolved once, from the two people tables: an id by id, a login by the local
  part of the stored email, and a stored name by its ascii fold. Measured over
  180 days of requests: 47 of 49 distinct principals resolve, 38 of them to a
  different string than the screen printed — including the ranked lists, where
  the folded and unfolded spellings of one colleague were two rows with the
  count split between them. What cannot be resolved is returned unchanged: a
  service principal belongs to no person, and the auto-approver is not sent
  through the lookup at all.
- **The audit trail names the requester, not the actor.** Its one name slot
  held the admin who pressed Approve, which for a run of auto-approvals is the
  same three words repeated; the person who asked for the query is what an
  auditor scanning the trail is looking for, and 76% of the rows the screen
  shows by default carry one. The actor is still in the rail, one click away.
- **The Slack sign-in names the workspace.** The authorize request never sent
  `team`, so Slack rendered the account picker — and because it had to ask, it
  asked for consent too, on an app the workspace had already authorised. It is
  the stricter flow as well as the quieter one: somebody in two workspaces
  could previously pick the wrong one and learn it later from
  `wrong_workspace`.
- **The masking screen was redesigned.** Two lines per exemption grouped by
  server, with the full story one click away; add another column without
  walking the whole form again; several columns of one table at once under one
  reason; and the form shows how that table is really queried before you decide
  about joins.

### Fixed

- **A view is listed under Views, not under Tables as well.** `GET
  /connections` dropped the relation kind, so the tree drew every relation
  under "Tables" and then drew the views again once the lazy schema load
  answered. One real database read "Tables 41" for 2 tables and 39 monitoring
  views; 77 of 84 catalogued databases were mixed that way, 2,968 views in
  total. The refs now carry the catalog's own relkind and one shared helper
  decides the branch, so the tree and the fleet search cannot drift apart.
- **The admin Connections screen stopped throwing.** `qhEndpointHover is not
  defined`, every open since 2026-09-10: the helper was declared in one file,
  called from another, and never put on `window`. The raw prototype hides that
  completely — every top-level declaration is a global there — while the built
  bundle gives each file its own module scope. Two guard tests now read the
  shipped sources and fail on a name that is used but neither declared nor
  exported, and on two files that declare the same top-level name.
- **A foreign table is listed** alongside ordinary tables in the browser.
- **An edit that changes nothing says so.** The endpoint answered
  `changed: false`, writing and auditing nothing, while the screen announced
  "Exemption updated." — which is a screen about reducing protection claiming
  an edit that did not happen. The audit row is still written for every edit,
  including a reason corrected to itself; only a change to what the exemption
  reaches sends a notification.

## [1.0.31] — 2026-09-10

Masking exemptions became writable from the panel, and two ways masking was
wrong in the stored model were found while building it. Patch bump: the
`bot_config` keys and the audit contract are unchanged.

### Security

- **A schema on a narrow exemption no longer widens it.** `pii_masking_exemptions`
  rows that name a table or a column also carry a schema, to NARROW what they
  cover. One branch read it as though the row were schema-scoped, so a single
  exemption on one column lifted masking for EVERY statement whose tables were
  all schema-qualified in that schema. Measured on the live fleet before the
  fix: three such rows on two targets, firing on 25 real requests since
  2026-08-13. The columns those requests returned were already exempt by their
  own rows, so nothing measurable was exposed — the hole was in what the next
  query would have got. It mattered more than its age suggests because the new
  Add-exemption form makes a schema mandatory on the column rung, which would
  have made the accident the default.
- **A schema now narrows what it names.** The mirror of the same confusion:
  `table_name` matches the bare name, so an exemption for one schema's table
  also freed a same-named table in another. A row that names a schema is held
  to it, and the rule can only ever refuse — a statement that writes the table
  unqualified, or that will not parse, is unchanged, because refusing there
  would start masking data that comes back unmasked today. Replayed over 3,211
  completed requests: no answer changed.

### Added

- **Masking exemptions can be written from the panel.** Create, switch off,
  remove, the catalog the pickers read, and a preview. Super-admin only; every
  change is audited under data protection. The rung is the contract: `scope` is
  what the operator confirmed on screen and everything narrower is forced to
  NULL, so a stale form field cannot store a different row than the sentence
  that was agreed. Fleet-wide comes from the scope, never from a missing
  connection.
- **A preview that reads one real row.** A generated `SELECT … LIMIT 1` on the
  read-only credential, in a read-only transaction, at most six columns, and
  only for a table a real query has touched in the last 30 days — that query is
  evidence the table is live, never re-executed. Both sides of the diff run
  through the real masker, so the screen cannot promise an outcome the executor
  would not produce.
- **Masking changes notify the admins** (migration 122). Twenty-one triggers
  covered the tables deciding who may READ what; the table deciding what they
  SEE had none. It is the one with no subject of its own, so the event carries
  no user and the poller fans it out to the people who can undo it.
- **Targets carry the cloud account they live in.** Resolved from the RDS
  inventory of every reachable account rather than from the endpoint, whose
  middle token is an opaque resource id and not an account number. Shown as one
  line on the hover already under a connection name.

### Fixed

- **A database that is not on the server named with it is refused at submit.**
  Nothing checked the pair; the catalog knew and the check was already written
  for auto-approve grants. Pre-flight also stopped failing open on
  `database "x" does not exist`, which arrives as a connection error and was
  being treated like a timeout — an answer that can never change waited on an
  approver and then failed at execution.
- **The database select is read by name, not by a prefix scan.** Its action_id
  is salted with the target so Slack re-fetches its options, and the block can
  then hold more than one entry.
- **The connections payload asks four questions, not one per row.** 313
  statements and 656ms became 9 and 61ms for a 50-connection reader; the admin
  listing went from 121/231ms to 5/18ms, byte-identical output.
- **The hourly catalog refresh diffs instead of rewriting** (migration 123). It
  deleted and re-inserted every row each hour: 172,450,815 `schema_columns`
  rows written over the life of a table holding 156,736. A full fleet refresh
  now writes none of them.
- **The audit search uses an index** instead of reading every row.
- **Every authority question has one answer** whichever door it is asked
  through.

## [1.0.30] — 2026-09-09

The audit trail rebuilt on a derived vocabulary, and a masking ladder that can
express every reach the model already stored.

### Added

- **The Audit trail, on categories nobody has to maintain.** The screen used to
  need a chip per action name, which is why it showed 37 of 132. Every row now
  carries a category and an effect derived server-side from its action name by
  an ordered pattern table (migration 118), so a new action type inherits a
  category for free and one nothing claims is counted as `unclassified` rather
  than filed under something it is not. Measured: 47 patterns classify 121 of
  132 action types, and the 8 that stay unclassified are visible on screen as a
  count instead of being absorbed.
- **Three dimensions instead of one.** Seven kinds (requests, data protection,
  access, connections, configuration, usage, unclassified), six effects (added,
  changed, removed, read, decided, ran) and three actor kinds (a person, the
  auto-approver, a background job). *Data protection x Read* is every time
  somebody looked at unmasked personal data — a question the old vocabulary
  could not ask. *Requests x the auto-approver* is what was approved without
  anybody looking, which is 78% of that category.
- **Every count carries its denominator, and every exclusion says its size.**
  Two slices are excluded by default and both are stated with a control to
  include them: the per-request lifecycle the queue and history screens already
  show, and product usage. Including the first triples the list, so it says so.
  Chips state the count they would produce, because a filter whose size you can
  only learn by clicking it is how a bucket stays unexamined.
- **A detail rail for the payload**, with the request block first — 77% of rows
  have one — the raw action name shown as the evidence the category was derived
  from, and the authority a machine acted under named where it can be resolved.
- **Two more rungs on the masking ladder**: a whole table, whose absence forced
  the wider "whole schema" choice, and fleet-wide, which the stored model always
  allowed and the screen could not produce. Fleet-wide is gated harder than
  anything else there: off the top of the ladder, a typed confirmation, and
  restricted to super-admins to start.

### Fixed

- The masking-exemptions screen listed every reach except the two it could not
  name, so one live exemption could be read but not reproduced.

## [1.0.29] — 2026-09-09

The release the access-model rewrite landed in. Authorization now resolves from
nine tables instead of seventeen, a third door opened for programs, and the
screens that read the new model stopped disagreeing with the routes that write
it.

### Added

- **The nine-table access model, switched on.** `principal`,
  `principal_identity`, `principal_credential`, `team`, `team_member`,
  `access_grant`, `role_assignment`, `principal_setting` and `tier` replace
  seventeen tables whose relationships were implied rather than declared. The
  old tables were copied in, verified row by row against a snapshot tool built
  for the purpose, and reads moved over behind one `bot_config` key
  (`access_model_v2`) so the switch could be thrown and thrown back. Legacy
  writes are projected into the new tables for as long as both exist, and
  authorization-change notifications fire from either side.
- **A scoped approver is told a request is waiting for them.** An approver whose
  authority covers one team or one target used to learn about a pending request
  only by opening the queue.
- **The Roles screen.** Who can approve what, as sentences rather than three
  chips the reader has to assemble: fleet-wide admins, team-scoped approvers,
  their tier ceilings and their expiry dates, with the rows that are mirrored
  from the old table marked as such. Readable by any admin; writable by a
  super-admin.
- **An org-structure importer.** A generic loader for team/person/service
  structure exported from an internal service catalog, plus the inventory tables
  it lands in, so team membership and the team that owns a target can be
  reconciled from the system of record instead of typed twice. A target's owning
  team is now a relation, not a join computed at request time.
- **A team's lead is synced as an approver for what that team owns**, and may
  approve their own team's requests and only their own.
- **An MCP surface — a third door, for programs.** Alongside Slack and the web
  UI, QueryHub speaks the Model Context Protocol over stdio: six tools
  (`list_connections`, `describe_database`, `classify_sql`, `submit_query`,
  `query_status`, `fetch_result`). It resolves the caller's own grants, refuses
  what they may not run, masks what comes back and audits every step. Off by
  default (`mcp_enabled`), read-only by default (`mcp_max_tier`), and an
  auto-approved query returns its first rows inside the same call — measured at
  1.4 s end to end.
- **A masking-exemptions screen.** Every place PII masking is deliberately
  switched off, super-admin only: what each exemption reaches, whether it also
  stops reading the values, whether it survives a join, which name rule caught
  the column in the first place, and the reason somebody wrote for it. Read-only
  in this release.
- The browser Back button now walks the app instead of leaving it, and dismisses
  the topmost modal before navigating.

### Changed

- Admin tables scroll inside their own box, so a wide table no longer scrolls
  the page sideways.
- `describe_database` and the schema response ask about PII once per response
  once per response instead of once per column, so the rule catalog is read once
  no matter how wide the table is.

### Fixed

- **A withdrawn request stayed on the admin queue** when it was withdrawn from
  the web: the buttons were still on the card, and pressing one answered
  "already decided". The web path now retires the admin cards the way the Slack
  path does, and the withdrawal is written to the audit trail.
- **The audit screen was hiding most of what it exists to show.** It filtered
  `audit_log` through a hand-written list of 37 action names against 132 real
  actions, so most of them were invisible — a requester withdrawing a request,
  masking being switched off for a column, every auto-approve grant. The filter is
  inverted: everything is shown except the per-request lifecycle the queue and
  history screens already show. Visible action types went from 37 to 123.
- Six admin routes wrote to the legacy tables while the screens above them read
  the new model — creating, renaming and deleting a team, setting a person's
  teams, copying access, and effective access. An emptied table answers
  "nothing" rather than failing, so none of them surfaced as an error. The
  Teams list was the dangerous one: it served new-model ids against an old
  sequence, so a few more teams would have made a rename edit a different team
  than the one on screen, silently.
- `/sql whoami` and `/sql roles` named no teams at all after the switch: the
  view behind them read only the legacy tables. It unions both models now, which
  needs no switch to be correct — whichever side is empty contributes nothing.
- Halting the fleet from Slack wrote no audit row, though the same action from
  the web did.
- The access-request card's copy-paste recipe named tables that no longer exist,
  and the flow accepted a request against the control-plane database it must
  refuse.
- An admin's own auto-approve waiver was ignored, and a drift gate counted
  tombstoned rows as live.

### Security

- **A tier ceiling admitted every tier.** A ceiling that could not be read
  resolved to rank 99 — a value chosen to fail closed on the *request* side of
  the comparison, which on the ceiling side meant "anything is below this".
  Both sides now fail closed, and the request's tier is derived by the engine-
  aware shared rule rather than from a display label that reports `ro` for a
  blocked statement.
- Two repository gates stopped telling the truth after the access-model cutover:
  the sensitive-data scanner's dynamic denylist and the copy-verification gate
  both read only the old tables, so a name that existed only in the new model
  was not on the denylist. Both read both models now, so a name that lives only in
  the new one is covered.

## [1.0.28] — 2026-09-06

### Added

- **Identity assertions from an internal developer portal.** The `/api` routes
  accept an Ed25519-signed `X-IDP-Assertion` header bound to the exact request
  (method, path and query, body) and act as the asserted principal, so a portal
  can proxy QueryHub for its users without a browser session. Off by default
  (`idp_assertion_enabled`), keys in `idp_public_keys`, replayed assertions
  refused through a short-lived `jti` ledger (migration 101).
- **A principal reconcile endpoint** that the portal's sync account, and only
  that account, may call to enable and disable requesters from its directory.
  It never writes to the `admins` table, refuses a list that would disable
  everyone, reports addresses it cannot resolve, and writes an audit row
  (`idp_principal_sync`) on every run.
- **A notification outbox** (migration 102) recording each pending request and
  the admins told about it, with two admin-gated routes for the portal to poll
  and acknowledge, so a portal can show an approvals queue without owning an
  admins table of its own. Slack notifications are unchanged.
- Requests that arrive this way carry `origin = idp` in the request record and
  the audit trail, next to `slack` and `web`.

## [1.0.27] — 2026-09-01

### Added

- **The person-first access screen.** Open a person and see everywhere they stand
  — their own grants, what their teams give them, what they reach as an admin,
  and any auto-approve window — resolved by the same resolver a submission uses,
  so the screen cannot disagree with what runs. Granting the same access to
  several people is one panel and one call, and a refusal keeps the list on
  screen because nothing was written. Copying someone's access shows exactly what
  would be written and revoked, by server name, before anything happens.

### Fixed

- **A modal taller than the viewport trapped its own buttons.** The overlay is
  fixed so the page cannot scroll, the modal clipped, and the body had no
  overflow of its own — so editing a connection put Save and Cancel off-screen
  with nothing to reach them. One rule on `.qh-modal`, shared by all eight.
- **Two fields the new screen reads were missing or the wrong shape**: the
  copy-access dry run did not return `wouldCopyAutoApprove` (an absent field
  draws silence, which reads as "none" for the one option that skips human
  review), and `autoApproveCopied` returned names where the client counted. It is
  a count now, with `autoApproveCopiedTargets` beside it.

## [1.0.26] — 2026-09-01

### Added

- **`POST /admin/grants` takes `subjects[]` — several people, one call, all or
  nothing.** N calls could leave three of five written, which the grants table
  cannot describe because it records what exists and never what was meant. Every
  id is validated before a statement runs and the writes share one transaction.
  `subject` still works unchanged.
- **`copy-access` can replace, preview, and carry auto-approve.** It only ever
  added, so "make this person match that one" was a sentence the API could not
  say. `mode: 'replace'` revokes what the source lacks; `merge` stays the default
  because it cannot take anything away. `includeAutoApprove` is off unless asked
  — that grant skips human review. `dryRun` names both sides by server alias and
  writes nothing, rollback verified against a live database.
- **`scopeTargetsAll` / `scopeTeamsAll`** on `effective-access`, so "every
  target" and "no target" stop being NULL and `[]` told apart by shape.

### Changed

- **`GET /admin/people` lists every principal an admin can name**, with `enabled`
  and `kind`: a disabled requester, an admin with no requesters row, and anyone
  who exists only as a grant subject. A name missing from a picker is not a name
  anyone gives up on — they type the id, and a typo grants access to a principal
  that cannot sign in.

### Fixed

- **`effective-access` cost 449 round trips and 780ms at p95**, because it asked
  the single-target resolver once per target. `teams.effective_grants_for_user`
  answers for every target in two queries with the same precedence; the endpoint
  is 36–55ms. A test compares the two resolvers for every principal on every
  target — 3190 comparisons — so they cannot drift apart later.

## [1.0.25] — 2026-08-31

### Changed

- **`/sql grant` takes several people at once, and starts with whoever the DM
  is with.** Access is handed to a group as often as to a person, and the picker
  took one user — so that was one pass each with the target, tier, databases and
  reason retyped every time. It is a multi-select now, and the loop is around
  people rather than inside the notification: every grantee still gets exactly
  one DM covering all the targets, and one person failing no longer costs the
  others their grant (the admin's confirmation names who got it and who did
  not). The command is also usually typed in the conversation where the access
  was asked for, so the modal arrives with that person already selected — one
  partner from a direct message, everyone from a group DM. A DM with the bot
  preselects nobody, a channel costs no API call, and a Slack failure just
  opens the modal empty.

## [1.0.24] — 2026-08-31

### Changed

- **Adding an auto-approve grant picks the person.** The subject field was a
  bare text box labelled "user or team", so an admin typed a Slack id from
  memory — and a typo is accepted: the server takes any well-formed principal,
  the grant is written, it matches nothing, and it sits in the table looking
  granted. It is the same picker the Grants form already used: search by name or
  by id, arrow keys, and a pasted principal id is still allowed but checked
  against the directory first. The placeholder also stops offering teams, which
  `auto_approve_grants` has never had a column for.

## [1.0.23] — 2026-08-28

### Fixed

- **The replica sync could skip its own marker.** The check for an existing
  `Upstream-Commit:` trailer was a substring test, and the first sync message to
  explain the mechanism in prose contained the words — so the append was
  skipped and that commit reached the replica unmarked, leaving the next sync
  unable to detect downstream work, which is the one thing the marker is for. A
  trailer is now a line that starts with the key.

## [1.0.22] — 2026-08-28

### Added

- **Work written in a downstream replica can come back up, and a sync will not
  overwrite it.** `sync_downstream_replica.py` rebuilds a replica's tree from
  this repository — safe while the replica is only read, and no longer safe once
  somebody develops there. A rebuilt tree that lacks their files is a valid
  tree, so the sync would have deleted downstream work with no error and no
  conflict. Every sync now plants an `Upstream-Commit:` trailer naming the
  commit it was built from, and refuses while the replica carries commits after
  it, printing them and the import command. `scripts/import_from_replica.py` is
  the other direction: it takes the paths those commits touched, minus the
  replica-local ones, onto a branch here, keeping every author as a
  `Co-Authored-By` trailer. It never pushes and never merges.

## [1.0.21] — 2026-08-28

### Changed

- **A request now says which surface it actually came from.** `requests.origin`
  is plain text with no constraint, so a new surface can start writing its own
  value the day it exists — but every place that displayed it asked the same
  two-way question, `web` or else Slack, in four spots: the admin DM, the audit
  feed, and the approval-queue chip twice. A request from any third surface
  would have been shown to approvers as **Slack**, on the one field whose entire
  job is to name the door it came through. Result delivery asked that question
  too, so such a request would also have had its CSV DM'd into Slack. The
  vocabulary now lives in one module; an unfamiliar origin renders as itself
  rather than as one of the first two, and `idp` is named there ahead of the
  integration that will write it.

## [1.0.20] — 2026-08-27

### Fixed

- **The editor's autocomplete was clipped by the results panel.** Typing near the
  bottom of the editor showed half a suggestion list, cut off exactly where the
  Results panel starts. Not a stacking problem — the editor pane is
  `overflow: hidden`, and an ancestor's clip ignores z-index, so raising it could
  never have helped. The list is now portalled to the document body and
  positioned in viewport coordinates: it floats above the panels, opens upwards
  when there is no room below, is clamped horizontally, and closes when anything
  scrolls out from under it. Keyboard selection and the accept-on-mousedown
  behaviour are unchanged.
- **The built app would have thrown where the prototype did not.** `window.ReactDOM`
  in the Vite bundle is a hand-written shim over the prototype's CDN globals, and it
  exposed only `createRoot` — `createPortal` lives in `react-dom`, not
  `react-dom/client`. A test now scans every design-owned component for
  `ReactDOM.<member>` and fails if the shim is missing one.

## [1.0.19] — 2026-08-26

### Fixed

- **The read-burst nudge counted opened tabs as queries.** Someone who ran one
  read was told they had run three, and offered an auto-approve window for
  `target #None (all dbs)`. Opening the `/sql` modal reserves a request id as a
  `draft` — no target, no database, no SQL — and the burst counter read the
  `requests` table directly instead of through the view every other caller uses,
  so two abandoned tabs plus one real query made three. The `#None` came from
  the same rows: the scope label picks the most common target in the window, and
  the drafts had none. Drafts are now excluded in the query itself, because
  `LIMIT 50` would otherwise fill with tabs and push the real requests out of
  the window. A blank query no longer counts as a read either — `required_mode`
  calls it RO, which is the right fail-safe when deciding what to run and the
  wrong answer when counting what was run.

## [1.0.18] — 2026-08-25

### Security

- **A password in a query no longer survives in request history.** A
  role-creation script carries the new password as a literal, and the whole
  statement is stored so the request can be reviewed, audited and re-run — so
  the credential stayed readable for as long as the row did, while the account
  it created was live. Migration 100 masks those literals the moment a request
  reaches a terminal state, replacing them with `PASSWORD '***REDACTED***'`.
  Timing is the point: the executor still sends the real statement, an approver
  still sees what they are approving, and a request parked in
  `awaiting_dba_manual` — where a human has yet to run the statement by hand —
  keeps its text until the admin closes it out. It is a trigger rather than
  application code because nineteen places across five modules write a terminal
  status, two processes run the executor, and an operator can update the row
  from psql.

## [1.0.17] — 2026-08-25

### Added

- **A PowerShell twin of the break-glass lockout.** The machine an operator
  reaches for in an incident is usually their own, and asking it for Python and
  psycopg first is one dependency too many.
  `scripts/breakglass_lockout.ps1` does the same three moves — NOLOGIN, a new
  random password, then the open sessions — through `psql`, reading the same
  `--dump-plan` file and connecting with the operator's own superuser. The SQL
  goes in on stdin rather than `-c`, so a generated password never lands in the
  process list or the shell history, and each `ALTER ROLE` is guarded by
  `IF EXISTS` so a server that does not have a given login is a no-op instead
  of an error that stops the rest of the run.

## [1.0.16] — 2026-08-25

### Added

- **The break-glass lockout can run from a second machine.** The incident may
  be that the bot host itself is the problem, but the fleet list lives in the
  metadata DB — so a copy elsewhere would have needed `BOT_DB_*` and the master
  key just to learn which servers exist, which means spreading the bot's
  secrets around to prepare for the day they leak.
  `--dump-plan fleet.json` exports hosts, ports and login *names* and nothing
  else (mode 600, no credentials, no key material). `--plan fleet.json
  --admin-user <you>` then locks the fleet with your own superuser credential,
  opening no metadata DB at all — it now starts even where the bot's modules
  cannot be imported, which is what a machine without the bot env looks like.

## [1.0.15] — 2026-08-25

### Security

- **The elevated role is entered for DDL, and only for DDL.** A super-admin's
  read or write ran under `SET ROLE` to the elevated role on every target that
  configures one, which is more authority than either tier needs: reads inherit
  `pg_read_all_data`, and writes hold their own grants. The role exists because
  the bot's login owns nothing and DDL has to. Reads and writes now connect as
  their own login and assume nothing, and the audit row says so.

### Added

- **A break-glass lockout for the whole fleet.** `scripts/breakglass_lockout.py`
  shuts every QueryHub database login out of every target in one move: NOLOGIN,
  then a new random password that is not stored, then the sessions already open.
  Dry run by default. `--admin-user` performs the lockout as a named superuser,
  for the case where the bot's own credentials are what is not trusted, and
  `--kill-switch` pauses submissions at the same time so the pause has a reason
  attached to it rather than looking like an outage.

## [1.0.14] — 2026-08-24

### Added

- **The result switcher moved onto the Results tab and names every result.**
  `‹ Results 1/3 ›` reads as one tab, the label opens a list of all N, and
  `Alt`/`⌥` + arrow steps between them. Each row is labelled with a one-line
  snippet of the statement that produced it, captured from the split the
  executor ran, so a label cannot describe a different statement from the one
  on screen. `GET /queries/{id}/result` carries `statements: [{n, kind,
  snippet}]` for the whole run on every statement's response.

### Fixed

- **A field the API added never reached the grid.** `qhApi.result` assembled
  its return object key by key, so anything the list did not name was dropped:
  `colTypes` first, silently masked by the schema-name fallback, then
  `statements[]` the week it was added. The payload is spread first now, with
  the named fields as defaults on top and the client-side closures last. Six
  tests hold the rule, since the comment that described the trap had already
  failed to.

- **The notification bell reported decisions the reader had just made.** A
  super-admin approves their own submissions by definition, so every self-test
  left an item about their own approval. An approval or rejection by someone
  else, an auto-approve grant and a finished scheduled run all still report.

### Changed

- **A server's name no longer collapses in the action bar.** With three chips
  in the row the target strip shrank to the environment tag; the name keeps
  its width and the ellipsis falls on the database instead, with room coming
  from the secondary buttons dropping their labels below 1120px.


### Added

- **The Messages tab says how many statements ran, and repeats what the server
  said.** A script that returns no rows finished as "Done — 0 row(s)" with an
  empty grid: nine statements had run and the only record was in the audit log,
  so the script was submitted twice. Runs now carry a summary
  (`3 statements executed: 3 DO.`) and every `RAISE NOTICE` / `WARNING` the server
  emitted, attributed to the statement that raised it. For an idempotent role
  script those notices are the entire output. Postgres only; capped so a
  runaway loop cannot fill the row.

## [1.0.13] — 2026-08-22

### Added

- **Ask for access by picking from a list.** `GET /requestable` returns the
  enabled connections a person cannot reach, with only the databases they do
  not hold. Excluded: the control plane, the maintenance databases, disabled
  targets, already-granted pairs. `POST /endpoint-requests` takes a
  `connectionId` from that list and treats it as authoritative — unknown or
  disabled is a 404, a database not on it a 400. No fallback to free text; the
  fallback is what produced requests nobody could resolve. Free text still
  works for a database QueryHub does not have yet.

- **Add a person from the Grants screen.** The subject field takes a principal
  QueryHub has never seen. `GET /admin/people/resolve` names them first, so a
  mistyped id is caught before the grant exists. No create-person endpoint:
  granting access is what adds someone.

### Fixed

- **`SELECT * FROM pg_roles` failed with "Timestamp too large (after year
  10K)".** Cause: `rolvaliduntil` is `infinity` for any role created without
  `VALID UNTIL`, and psycopg rejected the row rather than the cell. Fix:
  infinite values load as the literal `infinity` / `-infinity`, for `date`,
  `timestamp` and `timestamptz`. Real timestamps are unchanged.

- **An `EXPLAIN` from the web UI showed nothing.** Cause: the plan went out as
  a Slack code block and was never stored, and web results are not also DM'd —
  so it was discarded, not merely unshown. Fix: a lone `EXPLAIN` writes the
  plan as a one-column result too, and `explain_max_chars` clips the code block
  instead of the only copy.

- **Copy-access half-onboarded an unknown principal.**
  `POST /admin/people/{id}/copy-access` wrote team memberships and grants but
  no `requesters` row, so every query was refused by the whitelist gate and the
  person was invisible in the people list. It whitelists first now, the way
  `grants.grant` always has.

### Changed

- **A server's name is shown whole.** The head every target shares was dimmed,
  and dropped entirely in a narrow pane. A target name is an identifier people
  copy into psql; it renders at full contrast now and truncates like any other
  name.

- **A disabled target looks disabled** — greyed engine logo, dimmed name, in
  the tree, the editor's autocomplete and the admin Connections table. It still
  answers, with stale data, so it must not look live.

## [1.0.12] — 2026-08-21

### Added

- **A warning before a standing grant lapses.** A DM 24 hours and again 4
  hours before the deadline, to the people who will actually be refused — the
  grantee, or every member of the team for a team grant. Sent once per grant
  per threshold, recorded in a ledger keyed by the deadline itself, so
  extending a grant re-arms the warnings rather than suppressing them for
  good. Off unless `grant_expiry_warn_enabled` is on; the thresholds are
  configurable.

- **A resolved access panel per person**, answered by the resolver a
  submission uses rather than re-derived for the screen: every server, the
  tier, the team it came from, and any expiry. An admin's blanket reach is
  reported as its own source instead of being flattened into a direct grant
  that nothing can revoke.

- **Per-statement export.** `result.csv` and `result.xlsx` take
  `?statement=N`, so a download can follow the result the reader is looking
  at rather than the first one.

### Fixed

- **A query refused for expired access said only that you were not
  authorized** — the same sentence as for a server never granted. The
  refusal now carries its own code (`access_expired`) and the date as a
  field, rendered in the configured display timezone so it cannot name the
  wrong calendar day near midnight.

- **Export on a multi-statement result served the whole archive under the
  media type for an Excel workbook.** A zip arrived claiming to be a
  spreadsheet, with a correct-looking filename. A zip now says
  `application/zip`, and asking for one statement returns one table.

- **A saved query or history row pointing at an unreachable server gave no
  reason, and three different reasons were indistinguishable.** Two produced
  the same confident-looking alias and the third a bare id that read like
  one. Both endpoints now carry `connectionState`
  (`ok | no_access | retired | gone | none`) alongside the alias.

- **One DM per person instead of one per authorization row.** A bulk grant
  change produced a message per row; a burst is now a single DM naming the
  count, with a bullet per change. A lone change keeps its exact previous
  wording.

- **The granter column showed a raw handle** where the neighbouring table
  already showed a name, and two of three listings undid the
  enabled-first ordering they were given.

## [1.0.11] — 2026-08-20

Releases for 1.0.8 through 1.0.10 were never cut; their notes are in the
sections below and their content is included here.

### Fixed

- **A super-admin who asked for an unmasked result got a masked one.** The
  intent was recorded correctly on the request the whole time — it just never
  reached the code that acts on it. The column list handed to the executor is
  the executor's input, and `unmasked` had never been added to it, so the flag
  arrived as null on every path and masking took the default. A missing key is
  indistinguishable from an explicit "no" here, which is why nothing failed.
  Two other columns had drifted out of the same list; a test now walks the
  executor for every field it reads and fails if the list omits one.

- **A read-only query could be pushed to manual DBA execution by a comment.**
  The check that spots statements which cannot run inside a transaction
  searched the raw text, so a note *about* an index build — one that said the
  index was created WITHOUT `CONCURRENTLY` — matched the keyword it was
  denying. Comments and string literals are now excluded from that scan.

- **Multi-statement results showed nothing.** Running two queries in one
  submission stores a per-statement archive rather than a single table, and
  the grid refused to read anything else — so the header reported the rows
  returned while the view stayed empty. Results now serve per statement, with
  the first shown by default and the statement count alongside it.

- **An auto-approve grant scoped to "every database" never fired.** The form's
  own default posted a literal `*`, which is not a wildcard the matcher
  understands: the request fell through to manual approval exactly as if no
  grant existed, with an active grant sitting in the table looking correct. A
  database name that does not exist on the connection is now refused when the
  grant is created, rather than becoming the same silent no-op.

### Added

- **Copy one person's access onto another**, which is what onboarding actually
  asks for. Either by joining the same teams, or by writing out explicit
  per-user grants covering everything the source can reach — including the
  targets they reach through a team, which is where access is usually lost
  when membership is not copied. The tier can be overridden for the whole
  copy, so "the same servers, but read-only" is one request.

- **A resolved view of what any person can reach**, answered by the same
  resolver a submission uses rather than re-derived, so it cannot disagree
  with what actually happens at run time.

- **Schema refresh can target a single database** instead of re-reading every
  database on a connection.

- **The approval queue can group a batch.** Items of one batch submission
  arrive as separate rows; each now carries its position and the size of the
  batch it belongs to.

### Changed

- Auto-approve grants show the person's name, not just their Slack id.
- Disabled connections sort after enabled ones instead of alphabetically
  among them.

## [1.0.10] — 2026-08-10

### Fixed

- **The CI leak gate scanned all of history instead of the pull request.** The
  commit-message scan grandfathers a repository's pre-gate messages through a
  recorded commit; a fork or mirror that does not contain that commit fell back to
  scanning everything and failed on history nobody in the pull request wrote —
  unfixable without a force push. The job now derives the range from the event, and
  the tests cover the other direction too: a leak inside the scanned range still
  fails the build, including one introduced by the pull request itself.

## [1.0.9] — 2026-08-09

### Added

- **Word wrap in the editor** (⌥Z / Alt+Z, or the button in the tab bar). Off by
  default and remembered per browser. Line numbers keep one number per logical
  line and grow to the wrapped line's real height, measured rather than
  calculated — `break-word` breaks at spaces, so arithmetic is wrong on exactly
  the lines that wrap.
- **Functions and procedures are suggested.** The schema snapshot now scans each
  database's routines with a per-engine catalog query (`pg_proc` on PostgreSQL,
  `sys.objects` on SQL Server, nothing for an engine with no scan), so a routine
  can be completed at all. Accepting one inserts it called — `count_orders(`.
- **Frontend tests grew to 33** and run in CI, including the editor mounted in a
  DOM.
- **New-query +** sits at the end of the tabs and moves to the pinned cluster only
  when the tabs no longer fit.

### Fixed

- **`sys.dm_` suggested nothing** while the bare `dm_` worked: the system pool
  holds qualified names, so after a dot the token is only the tail. Accepting one
  replaces the token alone, and a foreign qualifier is no longer offered inside
  `sys.`.
- **`SELECT ... INTO t` was reviewed as a read.** It creates a table, and
  `CREATE TABLE AS` — the same operation, written differently — was always DDL.
  Now DDL on both engines.
- **`EXPLAIN` did not scan the statement it wraps**, so a blocked function could
  be named inside it while the bare call was refused. Both `EXPLAIN` forms scan
  now.
- **Disabling a target now stops queued and scheduled runs**, not just new
  submissions. A request already approved ran against a disabled host.

### Security

- Repository scanner fails closed when its denylist source is unreachable
  (it used to continue with a twentieth of its patterns and still report clean),
  and both leak gates now run in CI.
- All GitHub Actions pinned to commit SHAs; one was pinned to a mutable branch.
  Every CI and release job has a timeout, and CI declares least privilege.
- Frontend `nanoid` (high) and `postcss` advisories cleared.

## [1.0.8] — 2026-08-08

### Fixed

- **Run now honours a selection.** With text selected in the editor, Run — the
  toolbar button, F5 and Ctrl/Cmd+Enter alike — submits exactly that text instead
  of the whole tab. Previously only F8 did, so selecting one statement out of a
  multi-statement script and pressing Run submitted the entire script. A
  selection that contains only comments runs nothing and says so, rather than
  falling back to "run everything": highlighting a commented-out `UPDATE` must
  not execute the script around it. The rule lives in one function shared by all
  four entry points.
- **Autocomplete after a schema qualifier.** In a `FROM`/`JOIN`/`UPDATE` clause,
  `schema.tab|` offered columns — which cannot appear there — because the clause
  keyword was read from the word immediately before the caret, which is the
  qualifier itself. Accepting a qualified suggestion also wrote the schema twice
  (`dba.dba.whoisactive`), since the inserted text re-qualified while the replaced
  range covered only the bare name. A column pick after an alias still leaves the
  alias alone.
- **SQL Server icon.** The engine badge for SQL Server targets now uses
  Microsoft's current SQL Server 2025 mark.

### Added

- **Frontend tests.** `npm test` in `QueryHubWeb/app` runs `node --test` over the
  browser-side code, including the editor mounted in a DOM, and runs in CI after
  the bundle build. Frontend changes previously had no proof beyond compiling.

## [1.0.7] — 2026-08-06

### Added
- **A masking exemption can now be scoped to a schema and to super-admins.**
  The catalog matches column *names*, so an operator's own monitoring views come
  back mangled: a view exposing query text, host names or session owners trips
  the same rules a customer table does. Two new dimensions on
  `pii_masking_exemptions` carry it — `schema_name` and `super_admin_only` — and
  `target_server_id` becomes nullable so one row covers a whole fleet instead of
  drifting the moment a server is added.

  Both dimensions are needed. Scoping by the existing `table_name` would not
  work: it matches the **bare** name, so `dba.sessions` and `public.sessions` are
  one string to it and a toolkit exemption would unmask a business table that
  happens to share a name. And without the reader dimension the exemption is
  fleet-wide for everyone, which for `pg_stat_statements` means handing query
  literals to anyone with a grant.

  Two fail-closed rules: every table reference must be explicitly
  schema-qualified (an unqualified name resolves through `search_path`, so its
  schema is not knowable from the text), and every schema named must be exempt —
  one table from anywhere else and masking stays on for the whole result, the
  rule joins already follow. An unknown reader gets the strict answer, and so
  does a failed privilege lookup.

## [1.0.6] — 2026-08-06

### Added
- **Enabling a target without a credential is now refused.** Onboarding a
  discovered endpoint is two steps — make it visible, and give it a password —
  and only the first one is memorable, so the second gets skipped. Nothing used
  to complain: the target joined the picker, a grant could be issued on it, the
  tier badge rendered, and the schema snapshot failed quietly, so the tree showed
  a database with no tables. The failure surfaced on a user's screen instead of
  in the system. `set_enabled` is the single choke point every caller goes
  through, so the refusal lives there, names the target, and says what to do
  next; `force=True` still stages a target deliberately.
- `scripts/adopt_target_credential.py` — copies a credential from a sibling
  target for fleets where one database role serves every instance. It moves the
  **ciphertext**, so the secret is never read, printed or passed as an argument,
  and it refuses when the two targets use different role names, because that
  produces a login failure that looks like a password problem. Dry run by
  default; `--check` reports enabled targets that still hold the placeholder.

## [1.0.5] — 2026-08-06

### Fixed
- **The maintenance database could reach a user's sidebar.** `postgres` is hidden
  from every listing on purpose, and it is also `default_database` on most of this
  fleet. `GET /connections` filtered the hidden names *before* falling back to the
  default database, so a target with an empty catalog — which is every target
  between being enabled and its first schema snapshot — fell back to exactly the
  database meant to be invisible, past a filter that had already run. A user saw it
  in the tree. The filter now runs after the fallback and covers both paths in one
  pass, and a target left with nothing after filtering is dropped rather than
  falling back a second time.

## [1.0.4] — 2026-08-01

### Changed
- The GitHub Actions the workflows call are current again: `checkout` 4→7,
  `setup-node` 4→7, `download-artifact` 4→8, `login-action` 3→4.5.2 and
  `build-push-action` 6→7. No shipped code changes; this release exists so the
  build that produces the artifacts is not running on abandoned action majors.

## [1.0.3] — 2026-07-31

### Added
- **A reason field in the web editor.** Ported from the design source: a strip
  between the action bar and the editor, not a dialog in front of Run, because the
  reason is part of the request. Run stays enabled with an empty required field —
  it focuses and rings it rather than doing nothing, since a disabled button plus
  a keyboard shortcut is a no-op nobody can diagnose. The strip appears only on a
  settled classification and never disappears, so typed text is not destroyed by a
  keystroke that drops the statement back to read-only. Recent reasons are offered
  as chips and never prefilled.
- **`geography`, `geometry` and `hierarchyid` are readable.** They arrive as SQL
  Server's own serialisation, which we could only render as hex. The executor now
  asks the server for the result shape without running the query, and where one of
  those types appears, re-projects that column through `.ToString()` —
  `POINT (28.9784 41.0082)`, `/1/2/3/`. Read-only queries only, and the server
  decides: a top-level `ORDER BY` makes a query unwrappable, so the wrapped form is
  attempted and the original runs if it is refused.
- The sidebar makes room for long target names: the fleet-wide name head is folded
  and dimmed, an ambiguous database row names its server on a second line, and
  double-clicking the resizer fits the pane to its widest row.

### Fixed
- `POST /classify` publishes `requiresJustificationWhenReviewed` — named for the
  question it answers, "will a human read this?" The old
  `requiresJustificationWhenScheduled` is kept as an alias for one release. It gave
  the right answer for the wrong reason: a batch also always meets an approver, and
  a field whose name and meaning agree only by accident is one rename from a silent
  bug.
- `GET /admin/queue` emits `justification` as well as `reason`. The requester
  submits `justification`; the approver was reading the same value under a name that
  existed only because the queue mapper was written separately.
- The Slack modal no longer labels the justification field "(optional)". It is
  required for write and DDL unless an auto-approve grant covers it, and the hint
  now says so instead of promising the opposite.

## [1.0.2] — 2026-07-31

### Fixed
- The audit log described a web sign-in as "via slack", which read as a
  contradiction next to its own title, "Signed in to web". The data was right —
  someone signed into the web app using Slack SSO — but "via" was already taken
  in the same list, where a submitted request renders "via web" or "via Slack"
  to mean the surface it arrived from. One word, two meanings, one list. A
  sign-in now names its identity provider instead: "Slack SSO", "local account",
  or "<name> sign-in" for a provider this build does not know, because a login
  nobody can attribute is exactly what an audit reader needs to see.

### Changed
- The example image tag in `README.md`, `.env.example` and
  `docker-compose.install.yml` is now checked against `pyproject.toml` by a
  test. Naming a version rather than `latest` is deliberate — a gateway should
  not change underneath an operator because a tag moved — and the cost of that
  advice is a number that has to be maintained. It was not: the README went on
  advertising 1.0.0 after 1.0.1 shipped, which sends a reader to a real image
  that is not the one being shipped.

## [1.0.1] — 2026-07-31

### Fixed
- A query with an unbalanced quote came back as **"internal error"** instead of
  a syntax error. `ast_safety.check` catches `sqlglot.errors.ParseError` and
  fails closed with a message that already says "check for stray quotes" — but
  `TokenError` is a SIBLING of `ParseError`, not a subclass, and the tokenizer
  raises before a parse is attempted. So the one input the handler was written
  for was the one input that escaped it, all the way out of the API as a 500.
  Now the whole `SqlglotError` family is caught, with a backstop for anything
  new: a parser is a moving dependency on the submit path, and an exception
  class we have not seen must still refuse the query rather than reach a user.
- `POST /classify` reported `requiresJustification` as "DDL only", so the field
  the web editor is supposed to render was wrong for RW and ignored
  auto-approval entirely.

### Changed
- **A justification is no longer required when the request will be
  auto-approved.** The field's first reader is the approver, and an
  auto-approved request has no approver. The audit question is still answered:
  such a submission records the covering grant, and the grant carries its own
  reason. A SCHEDULED request stays exempt-free — its grant may lapse before
  the run time, which puts a human back in the loop.
- `/classify` now also publishes `requiresJustificationWhenScheduled`, because
  the client knows whether a schedule was picked and the endpoint does not.

### Known gap
- The web editor still has no justification input, so a user WITHOUT
  auto-approve cannot submit RW or DDL from the web. Slack's `/sql` modal has
  the field. The API contract this needs is now correct.

## [1.0.0] — 2026-07-31

The repository is public, and this is the first release built by the release
workflow rather than by hand. Nothing in the product changed for it: 1.0.0 marks
that the compatibility promise below is now in force, and that every install
path has been run end to end rather than assumed.

### Added
- A published container image: `ghcr.io/ahmetrende/database-queryhub:1.0.0`
  (and `:latest`). Pull it and set `QH_IMAGE` to skip the build. The compose
  file still builds from the checkout by default — an approval gateway is not
  something to upgrade by moving tag.

### Changed
- Comments and documentation no longer refer to a private tracker or a private
  working setup. 59 dangling issue IDs (`BUG-01`, `SEC-12`, …) are gone from the
  code, and `OPERATIONS.md` section 17 — which documented a script that is not
  part of an install — is now about the operational risk it was really pointing
  at: real identifiers leaking through screenshots, logs and result files.
- The two container CI jobs no longer skip themselves for dependency PRs. They
  are what catch a broken front door, which is what a dependency bump breaks.

### Fixed
- `MANIFEST.in` was missing `prune docs/screenshots`, so the screenshot index
  was pulled into the source distribution and `check_sdist_clean.py` failed on
  it — a blocking gate.

### Verified
- `docker compose -f docker-compose.install.yml up -d` builds, becomes healthy,
  applies the migrations and creates the bootstrap admin. All seven assertions
  in `scripts/ci_install_check.py` pass, including that the password printed in
  the container logs is good for nothing except changing itself.
- Full CI green on a public runner for the first time: unit tests on 3.11 and
  3.12, the real-database integration job, the optional-extras job, the vanilla
  (no-Slack) import job, the frontend build, and both container stacks.

## [0.1.0] — 2026-07-29

The initial cut, recorded for the history rather than as a shipped
artifact: no release, package or container image was ever produced for it.
`release.yml` did not exist yet, and a tag-triggered workflow is read from
the tagged ref, so nothing could have fired. 1.0.0 below is the first
release the workflow actually built.

### Install

- **Two-command install**: `docker-compose.install.yml` runs a released
  image against your own metadata Postgres, reading one `.env`. The first
  start generates the master key, applies the migrations and creates the
  admin account named by `QH_ADMIN_USER` — so nothing needs a shell in the
  container. Everything after that is in the UI: add a connection, test it,
  create a team, grant it.
- That first password is a **bootstrap** password: printed once if
  generated, and always carrying `must_change_pw`, so it is good for one
  login and cannot run a query. The account is created only if absent
  (`create_local_user.py --if-absent`), so a restart never resets a
  password the operator has changed.
- CI runs the install path end to end (`install-stack`, using the commands
  the README prints) and asserts it is clean — no demo accounts, no seeded
  connections — so the demo profile cannot leak into a deployment.

### Also in this release

- Access-request **approval auto-creates the per-user grant** it asks for
  (requester + target + database + requested tier; conservative on
  conflicts) — no more hand-run grant SQL for the common case.
- **Self-service password change** for local accounts, with a forced
  change flow for handed-off (`must_change_pw`) accounts.
- Hardening: access-request tier is taken only from the server-set field
  (never self-selectable via the reason text); the login brute-force
  throttle map is bounded; safe/typed `mypy` findings cleared.

### Core

- Admin-approved SQL gateway for **PostgreSQL** and **SQL Server**:
  submit → safety-check → approve → execute → deliver, with a full audit
  trail on every step.
- Three-tier execution (**RO / RW / DDL**): each query is classified
  (keyword + sqlglot AST second pass, engine-aware) and runs under the
  matching per-target credential — never above its tier.
- Team and per-user grants, scoped admins (tier / team / target), temp
  admin windows, time-bounded auto-approve exemptions, per-request
  fingerprint auto-approve, PII masking with column-level exemptions.
- Approving an access request auto-creates the per-user grant it asks
  for (requested tier, listed database) — no hand-run SQL for the
  common case.
- Pre-flight `EXPLAIN` with risk hints; per-user row limits; CSV/XLSX
  delivery with formula-injection neutralization; retention cleanup.

### Surfaces

- **Web UI** (FastAPI + React): SQL editor with schema-aware
  autocomplete, saved queries, history, server-paged results, Excel
  export, admin panel (approval queue, grants, config, audit, metrics),
  in-app notifications.
- **Slack** (optional `[slack]` extra): `/sql` modal, DM approvals,
  result delivery, schema browsing subcommands.

### Vanilla profile

- The base install runs with **zero external vendors** — no Slack, no
  cloud: web-only approvals, built-in **local accounts**
  (username/password; salted PBKDF2 hashes, never cleartext), guided
  installer (`scripts/install.sh`), brute-force login throttle.

### Security & operations

- Fernet-encrypted credentials at rest behind one master key; optional
  AWS Secrets Manager provider (`[aws]` extra).
- Migration ledger with checksums + advisory lock; graceful restarts
  that drain in-flight queries; MSSQL cross-database/linked-server
  blocking; `search_path` hardening; `SET LOCAL` value validation.
- Docs: install, prerequisites, configuration reference, architecture
  (core + adapters), auth/session design, operations, schema, known
  limitations. CI: lint + tests (3.11/3.12) + a no-Slack import gate.

[0.1.0]: https://github.com/ahmetrende/database-queryhub/releases/tag/v0.1.0
