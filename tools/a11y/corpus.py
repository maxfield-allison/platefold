"""Deterministic synthetic inputs; no household or customer documents."""

from pathlib import Path

from PIL import Image, ImageDraw


def create(root: Path) -> dict[str, tuple[Path, dict]]:
    root.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 240), "white")
    ImageDraw.Draw(image).rectangle((40, 40, 600, 200), fill="navy")
    image.save(root / "rectangle.png")
    prose = """# Accessibility specimen

## First section

ALPHA begins the intended reading sequence. Café and naïve test Unicode.

### Detail

BRAVO follows ALPHA. [Reference link](https://example.com/a11y) and
[internal link](#last-section) have distinct destinations.

- First list item
- Second list item

## Last section

CHARLIE ends the intended reading sequence.
"""
    semantics = (
        prose
        + """
## Table

| Item | Quantity | Status |
| --- | --- | --- |
| Apples | 4 | Ready |
| Pears | 2 | Pending |

## Informative image

![A navy rectangle centered on a white background](rectangle.png)

## Decorative image

![](rectangle.png)

## Described diagram

```mermaid
flowchart LR
    accTitle: Approval flow
    accDescr: Start proceeds to Review, then to Finish.
    A[Start] --> B[Review] --> C[Finish]
```

## Diagram without authored description

```mermaid
flowchart LR
    X[Open] --> Y[Close]
```
"""
    )
    long = "# Long document\n\n" + "\n\n".join(
        f"## Section {i:02d}\n\n" + (f"Paragraph {i:02d}: ordered prose and café. " * 40)
        for i in range(1, 25)
    )
    tiled = "# Tiled diagram\n\n## Process\n\n```mermaid\nflowchart LR\n"
    tiled += "    accTitle: Twelve step process\n"
    tiled += "    accDescr: Steps proceed in numerical order from step 1 to step 12.\n"
    tiled += " --> ".join(f'N{i}["Step {i}: review record"]' for i in range(1, 13))
    tiled += "\n```\n\n## After diagram\n\nDELTA follows the entire process.\n"
    sources = {"prose": prose, "semantics": semantics, "long-toc": long, "tiled": tiled}
    cases = {}
    for name, content in sources.items():
        path = root / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        settings = {"output_mode": "tiled"} if name == "tiled" else {}
        cases[name] = (path, settings)
    csv = root / "wide.csv"
    csv.write_text(
        ",".join(f"Column {i:02d}" for i in range(1, 25))
        + "\n"
        + "\n".join(
            ",".join(f"R{row:02d}C{col:02d}" for col in range(1, 25)) for row in range(1, 41)
        )
        + "\n",
        encoding="utf-8",
    )
    cases["wide-csv"] = (csv, {})
    return cases
