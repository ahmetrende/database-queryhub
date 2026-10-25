// Attaches the SQL formatter to `window`, where qh-sqlformat.jsx looks for it.
// The raw prototype has no bundler and no formatter. There, qhFormatSql returns
// null and the editor does nothing.
import { format } from 'sql-formatter';

window.__qhSqlFormatter = { format };
