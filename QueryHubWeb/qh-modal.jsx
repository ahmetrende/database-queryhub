// Shared modal shell: dialog semantics, focus management, Escape, Tab trap.
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

function qhFocusables(root) {
  return Array.prototype.filter.call(
    root.querySelectorAll(QH_FOCUSABLE),
    // offsetParent filters out anything display:none'd; a hidden control would
    // otherwise become an invisible stop in the tab cycle.
    (el) => el.offsetParent !== null || el === document.activeElement);
}

function QhModal({ onClose, children, panelClass, panelStyle, labelledBy }) {
  const panelRef = React.useRef(null);
  const restoreRef = React.useRef(null);
  const idRef = React.useRef(null);
  if (idRef.current === null) idRef.current = 'qh-modal-title-' + (++QH_MODAL_SEQ);

  React.useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return undefined;
    // Remember where focus came from so it can go back — closing a modal must
    // not dump the caret at the top of the document.
    restoreRef.current = document.activeElement;

    if (!labelledBy) {
      const title = panel.querySelector('.qh-modal-title');
      if (title) {
        if (!title.id) title.id = idRef.current;
        panel.setAttribute('aria-labelledby', title.id);
      }
    }

    const first = qhFocusables(panel)[0];
    (first || panel).focus();

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
      if (e.shiftKey && (document.activeElement === firstEl || !panel.contains(document.activeElement))) {
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

Object.assign(window, { QhModal, qhFocusables, qhUseDismiss });
