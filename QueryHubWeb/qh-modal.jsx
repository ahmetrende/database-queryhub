// Shared modal shell: dialog semantics, focus management, Escape, Tab trap,
// and Back-to-dismiss (see the history effect).
//
// Every modal in the app used to be a bare
//
//     <div className="qh-modal-overlay" onMouseDown={onClose}>
//       <div className="qh-modal" onMouseDown={stop}>…
//
// which looks fine with a mouse and is unusable without one: no role, so a
// screen reader announces nothing; no focus move, so the caret stays on the page
// behind; no Escape, so a keyboard user has no way out; and Tab walks straight
// out of the panel into the page underneath. This wraps all of that once so the
// six call sites keep their own head/body/foot markup and get the behaviour for
// free.
//
// The accessible name is taken from the panel's own `.qh-modal-title` row rather
// than a prop, so a modal that renames its title cannot drift out of sync with
// what gets announced.

// Elements that can hold focus. `:not([disabled])` matters: a disabled primary
// button (e.g. Save with an empty name) must not swallow the initial focus.
const QH_FOCUSABLE = [
  'a[href]', 'button:not([disabled])', 'input:not([disabled])',
  'select:not([disabled])', 'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

let QH_MODAL_SEQ = 0;

// Open modals, innermost last. Only the topmost one answers a Back — a stacked
// pair must close one at a time, the way Escape does.
const QH_MODAL_STACK = [];

function qhFocusables(root) {
  return Array.prototype.filter.call(
    root.querySelectorAll(QH_FOCUSABLE),
    // offsetParent filters out anything display:none'd; a hidden control would
    // otherwise become an invisible stop in the tab cycle.
    (el) => el.offsetParent !== null || el === document.activeElement);
}

// A field is a control the person types or picks in. When one of these took focus
// by itself (an `autoFocus` input), the modal leaves it alone.
const QH_FIELD_TAGS = ['INPUT', 'TEXTAREA', 'SELECT'];
const QH_NON_TEXT_INPUT = ['checkbox', 'radio', 'button', 'submit', 'reset', 'hidden', 'file', 'image', 'range', 'color'];
function qhIsFieldEl(el) {
  if (!el || QH_FIELD_TAGS.indexOf(el.tagName) < 0) return false;
  if (el.tagName === 'INPUT' && QH_NON_TEXT_INPUT.indexOf(String(el.type || '').toLowerCase()) >= 0) return false;
  return !el.disabled;
}
// Where focus goes when a modal opens (2026-10-24). This used to be the first
// focusable control, which in most dialogs is the close X, and in a confirm dialog
// is Cancel: the browser drew its default heavy ring on a button nobody had
// asked for, and it also took the focus away from an `autoFocus` field. Now:
//   1. a field that already took focus itself stays focused;
//   2. else the element marked `data-autofocus` (ConfirmRunModal marks Cancel, so
//      Enter still cancels);
//   3. else the first text field;
//   4. else the panel itself, which carries tabindex -1 and draws no ring.
const QH_TEXT_FIELD = 'input:not([disabled]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]):not([type="hidden"]):not([type="file"]), textarea:not([disabled])';
function qhInitialFocus(panel) {
  const active = document.activeElement;
  if (active && active !== panel && panel.contains(active) && qhIsFieldEl(active)) return active;
  return panel.querySelector('[data-autofocus]:not([disabled])') || panel.querySelector(QH_TEXT_FIELD) || panel;
}

function QhModal({ onClose, children, panelClass, panelStyle, labelledBy }) {
  const panelRef = React.useRef(null);
  // Read while rendering, before an `autoFocus` field inside the panel takes
  // focus at commit: after that, document.activeElement is already in the panel,
  // and closing would try to give focus back to a field that is gone.
  const restoreRef = React.useRef(typeof document !== 'undefined' ? document.activeElement : null);
  const idRef = React.useRef(null);
  if (idRef.current === null) idRef.current = 'qh-modal-title-' + (++QH_MODAL_SEQ);

  // The history effect runs on mount only, but `onClose` changes between
  // renders (ConnectionForm passes a no-op while a save is in flight), and a
  // Back must call the CURRENT one, not the one that existed when the modal
  // opened.
  const closeRef = React.useRef(onClose);
  React.useEffect(() => { closeRef.current = onClose; });

  // Back dismisses the topmost modal. Since PR #38 the app has real history
  // entries, so Back inside the admin panel changed the SECTION BEHIND an open
  // modal — a form left floating over a screen it no longer describes.
  //
  // A modal is not a location: it has no URL, cannot be bookmarked and must not
  // be reachable by Forward. So it gets a SENTINEL entry at the same URL
  // (`pushState(state, '', location.href)`) rather than a hash of its own.
  // Same URL means no `hashchange`, and both view-sync listeners in the app are
  // hashchange listeners — so the sentinel is invisible to them and the screen
  // behind cannot move while a modal is open.
  //
  // Closing by Escape / Cancel / × / Save spends the sentinel from the cleanup,
  // so history depth returns to exactly where it was before the modal opened;
  // otherwise every dismissed dialog would leave a dead Back press behind.
  React.useEffect(() => {
    const entry = { id: idRef.current, alive: true };
    QH_MODAL_STACK.push(entry);
    let spent = false;
    const mine = () => history.state && history.state.qhModal === entry.id;
    const push = () => { try { history.pushState({ qhModal: entry.id }, '', location.href); } catch (e) { /* file:// or a sandbox with no history */ } };
    push();
    const onPop = () => {
      if (QH_MODAL_STACK[QH_MODAL_STACK.length - 1] !== entry) return;
      // Landing ON our own sentinel is an inner modal spending its entry, not a
      // Back past ours.
      if (mine()) return;
      spent = true;
      if (closeRef.current) closeRef.current();
      // A modal that REFUSES to close (a save in flight) must not have spent the
      // entry — the next Back would then move the screen behind it.
      setTimeout(() => {
        if (entry.alive && QH_MODAL_STACK[QH_MODAL_STACK.length - 1] === entry) { spent = false; push(); }
      }, 0);
    };
    window.addEventListener('popstate', onPop);
    return () => {
      entry.alive = false;
      window.removeEventListener('popstate', onPop);
      const i = QH_MODAL_STACK.indexOf(entry);
      if (i >= 0) QH_MODAL_STACK.splice(i, 1);
      if (!spent && mine()) history.back();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return undefined;
    // Remember where focus came from so it can go back — closing a modal must
    // not dump the caret at the top of the document. The render-time read above
    // is the real one; this only fills in when that read landed inside the panel.
    if (!restoreRef.current || panel.contains(restoreRef.current)) restoreRef.current = document.activeElement;

    if (!labelledBy) {
      const title = panel.querySelector('.qh-modal-title');
      if (title) {
        if (!title.id) title.id = idRef.current;
        panel.setAttribute('aria-labelledby', title.id);
      }
    }

    qhInitialFocus(panel).focus();

    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        if (onClose) onClose();
        return;
      }
      if (e.key !== 'Tab') return;
      const items = qhFocusables(panel);
      if (!items.length) { e.preventDefault(); return; }
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      // Wrap at both ends. Without this, Tab past the last control moves into
      // the browser chrome and then into the page behind the overlay.
      // The panel itself can hold focus (no field to focus): Shift+Tab from it
      // would leave the dialog, so it counts as standing on the first control.
      if (e.shiftKey && (document.activeElement === firstEl || document.activeElement === panel || !panel.contains(document.activeElement))) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };
    // Capture phase: the editor and the autocomplete popup have their own
    // Escape handlers, and while a modal is open it must win.
    document.addEventListener('keydown', onKey, true);

    return () => {
      document.removeEventListener('keydown', onKey, true);
      const back = restoreRef.current;
      if (back && back.focus && document.contains(back)) {
        try { back.focus(); } catch (e) { /* element went away mid-close */ }
      }
    };
    // Mount/unmount only: re-running this would steal focus back to the first
    // control on every parent re-render (i.e. on every keystroke in a field).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="qh-modal-overlay" onMouseDown={onClose}>
      <div
        ref={panelRef}
        className={'qh-modal' + (panelClass ? ' ' + panelClass : '')}
        style={panelStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy || undefined}
        tabIndex={-1}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

// A dropdown that closes when you click away or press Escape — and NOT when the
// pointer happens to leave it.
//
// Both menus in the app used to be `onMouseLeave={() => setOpen(false)}` on a
// relatively-positioned wrapper whose popup sat 6px below it (`top: 34px` on a
// 28px button). That 6px belongs to neither element, so moving the pointer from
// the button down to the menu crossed it, fired mouseleave on the wrapper, and
// the menu vanished before it could be clicked. Reported as "the export menu
// disappears when I move the mouse to it".
//
// Closing a CLICKED menu on pointer-out is the wrong rule anyway: a menu opened
// by a click should stay until dismissed, so a slow or imprecise movement — a
// trackpad, a large screen, someone reading the options — cannot lose it.
//
// Usage: attach the returned ref to the wrapper element.
//
//   const ref = qhUseDismiss(open, () => setOpen(false));
//   <div className="qh-export" ref={ref}> …trigger + popup… </div>
//
// The popup must be a DOM descendant of that element for the outside test to
// work; `position: fixed` is fine, containment is by DOM, not geometry.
function qhUseDismiss(open, onClose) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => {
      // The trigger handles its own toggle; ignoring clicks inside means a
      // second click on it does not close-then-reopen.
      if (ref.current && !ref.current.contains(e.target)) onClose();
    };
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); onClose(); } };
    // pointerdown, not click: the menu must be gone before whatever was clicked
    // underneath reacts, and it covers touch as well as mouse.
    document.addEventListener('pointerdown', onDown, true);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('pointerdown', onDown, true);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open, onClose]);
  return ref;
}

Object.assign(window, { QhModal, qhFocusables, qhUseDismiss, qhInitialFocus, qhIsFieldEl });
