# QueryHub — Brand assets

The QueryHub mark: a database cylinder with a centered `>_` query prompt on its
face. Single flat accent color #C4603F,
flat, no gradients/shadows, squircle avatar.

## Colors
- Brand accent: #C4603F  (pressed #A24628, strong #B0512F)
- Dark surface: #181A20
- Mark on green bg is white; on dark/white bg it is green.

## Files

### svg/  (vector — scale freely, preferred for web)
- queryhub-avatar-green.svg   — primary: white mark on green squircle  ← Slack avatar
- queryhub-avatar-dark.svg    — green mark on dark squircle
- queryhub-avatar-white.svg   — green mark on white squircle (hairline border)
- queryhub-avatar-circle-green.svg — circle crop variant
- queryhub-mark-green/white/dark.svg — transparent mark only (no background)
- queryhub-lockup-light.svg   — horizontal icon + "QueryHub" wordmark (light bg)
- queryhub-lockup-dark.svg    — same, for dark backgrounds

### png/  (raster)
- queryhub-avatar-green-512.png   ← upload this as the Slack profile photo
- queryhub-avatar-green-192.png   — apple-touch-icon / PWA
- queryhub-avatar-green-48.png    — small avatar
- queryhub-avatar-dark/white/circle-512.png — variants
- queryhub-favicon-32.png, -16.png — browser favicon (transparent)

## Usage
- Slack profile photo: upload `png/queryhub-avatar-green-512.png`.
- Web favicon: `png/queryhub-favicon-32.png` (already wired into QueryHub.html).
- In-app the mark is drawn as inline SVG (QHMark component in qh-login.jsx) so it
  recolors with the theme — these files are for external/static use.
- Clear space: keep padding ≥ 25% of the mark's size around it. Don't recolor,
  rotate, add gradients/shadows, or stretch.
