# Duplicate Headings Fixture

Document: Heading mapping regression
Version: 1.0.0
Owner: Test Suite

---

# Alpha section

Body text for the first alpha section.

# Configuration

The word Configuration appears as a heading more than once in this document,
on purpose. Text matching maps every occurrence onto the first one; the
injected markers must keep them apart.

# Beta section

Body text for the beta section.

<div style="break-before: page;"></div>

# Configuration

This is the second Configuration heading, forced onto a later page. Its entry
in the contents list and the PDF outline must point here, not back to the
first one.

# Gamma section

Body text for the gamma section.

<div style="break-before: page;"></div>

# Configuration

And a third, later still.

# Delta section

Trailing content so the document does not end on a heading.
