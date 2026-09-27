# Icon set for a Windows application

Specification of the images to add to the generator, based on what it
already produces for the web.

> **Status.** Items 1, 2 and 3 are implemented in
> `icongen_windows.py`, and the set it produces is in use in
> `shelly_screens/assets/`. Items 4 and 5 remain — the simplified drawing
> for small sizes and legibility on dark backgrounds: they are a matter of
> drawing, not computation.

## Is the web set usable as is?

It works, but it is not complete. Three gaps, from the most visible to the
most subtle.

| | Current web set | Expected on Windows |
| --- | --- | --- |
| Transparency | **none** — everything is RGB PNG | alpha channel required |
| Sizes in the `.ico` | 16, 32, 48 | 16, 20, 24, 32, 40, 48, 64, 96, 256 |
| Internal format of the `.ico` | PNG for every size | BMP up to 48, PNG above |

## 1. Alpha channel — the most visible gap

The set's eight PNGs are **colour type 2 (RGB), with no alpha channel**. The
top-left corner of `icon-192.png` is `(16, 22, 26)`: a very dark, opaque
grey.

On the web, this does not show: the icon is displayed in a frame that
applies its own rounded corners. On Windows, nobody does that for it. The
icon therefore appears as a **solid square**, even where the drawing
suggests rounded corners — in the taskbar, the notification area, Explorer
and the title bar. On a light theme, the dark square stands out sharply.

**To produce:** the same images as **PNG colour type 6 (RGBA)**, with the
rounded corners truly transparent and a slight anti-aliasing of the edge
(a hard edge creates a visible staircase at small sizes).

## 2. Missing sizes

The `.ico` only contains 16, 32 and 48. Windows asks for more, and **resizes
whatever is missing itself** — hence a blurry result.

| Size | What it is used for |
| --- | --- |
| 16 | Title bar, notification area, compact lists |
| **20** | The same, on a screen at 125 % |
| **24** | The same, at 150 % |
| 32 | Desktop, taskbar, Alt+Tab |
| **40** | Desktop at 125 % |
| 48 | Explorer, "Medium icons" view |
| **64** | Explorer at 125–150 % |
| **96** | "Large icons" view |
| **256** | "Extra large icons" view, Properties window, installers |

The sizes in bold are the missing ones. The intermediate ones — 20, 24,
40 — are not a luxury: they match common scaling factors, and many screens
are set to 125 % or 150 %.

Each size must be **drawn at its own resolution**, not interpolated from a
larger one: that is precisely what we are trying to avoid.

## 3. Internal format of the `.ico` file

An `.ico` is a container: each size is stored either as **32-bit
BMP/DIB**, or as **compressed PNG**. The current set uses PNG everywhere.

The accepted rule:

* **up to 48 px: 32-bit BMP.** Windows 10 and 11 read PNG without any
  problem, but some older contexts do not accept it and display a blank
  icon. The overhead is negligible at these sizes.
* **from 64 px: PNG.** Uncompressed, a 256×256 BMP image weighs 256 KB on
  its own, and the file becomes absurd.

## 4. A simplified variant for small sizes

The drawing has two screens, a power symbol and lightning bolts. At 48 px
everything is readable. At 16 px — the actual size in the notification area
and the title bar — only a blob is left.

Polished icon sets provide a **separate drawing for small sizes**: a single
screen, or just the outline of the power symbol, with thicker strokes. This
drawing is used for 16, 20 and 24 px; the full drawing takes over from 32.

## 5. Legibility on light and dark backgrounds

The taskbar follows the Windows theme. Since the icon has a very dark
background, it stands out poorly on a dark taskbar — the Windows 11
default.

Two ways to handle it:

* a one-pixel **light outline** around the edge, which is usually enough;
* or **two variants**, light and dark, with the app choosing according to
  the system theme.

## 6. If an installer is planned

These images have nothing to do with the icon and are often forgotten.

| Tool | File | Dimensions | Format |
| --- | --- | --- | --- |
| Inno Setup | `WizardImageFile` | 164×314 | 24-bit BMP |
| Inno Setup | `WizardSmallImageFile` | 55×58 | 24-bit BMP |
| WiX / MSI | `WixUIBannerBmp` | 493×58 | 24-bit BMP |
| WiX / MSI | `WixUIDialogBmp` | 493×312 | 24-bit BMP |

24-bit BMP is imposed by these tools: no PNG, no transparency.

## What is useless on Windows

* `icon-180.png` — specific to the iOS home screen.
* `icon-maskable-*.png` — specific to PWAs, where the system crops the image
  into an arbitrary shape. Windows crops nothing.
* `manifest-icons.json` and `head-snippet.html` — web declarations.

Nothing needs deleting, though: these files do no harm, they are simply
ignored.

## Summary of what to implement

1. Switch **all** outputs to **RGBA** PNG, with truly transparent corners.
2. Generate sizes **20, 24, 40, 64, 96, 256** in addition to the existing three.
3. Assemble the `.ico` with **BMP up to 48** and **PNG from 64**.
4. Add a simplified drawing for **16, 20 and 24**.
5. Add a light outline, or a second variant for dark themes.
6. If there is an installer: the four BMPs in the table above.

Items 1 and 2 are the ones that show. The others are a matter of finish.
