// Geometry, clipping and motion probes for tests/test_ui_accessibility.py.
// Installed as an init script; every function reports problems as strings so
// a failing assertion names the element and the rule it broke.
window.__probe = (() => {
  const INTERACTIVE =
    'button, select, textarea, summary, a[href], [role="tab"], [tabindex]:not([tabindex="-1"]), input:not([type="hidden"])';

  const describe = (el) => {
    const bits = [el.tagName.toLowerCase()];
    if (el.id) bits.push(`#${el.id}`);
    const name =
      el.getAttribute("aria-label") ||
      (el.labels && el.labels[0] && el.labels[0].textContent) ||
      el.textContent ||
      "";
    const text = name.replace(/\s+/g, " ").trim().slice(0, 40);
    if (text) bits.push(`"${text}"`);
    return bits.join(" ");
  };

  const rendered = (el) => {
    if (!el.isConnected || !el.getClientRects().length) return false;
    // False inside a closed <details> and for visibility:hidden.
    if (!el.checkVisibility({ visibilityProperty: true })) return false;
    if (el.closest("[inert]")) return false;
    const dialog = el.closest("dialog");
    return !dialog || dialog.open;
  };

  // The top modal dialog is the only part of the page a user can reach.
  const scope = () => {
    const open = [...document.querySelectorAll("dialog")].filter(
      (d) => d.open && d.matches(":modal"),
    );
    return open.length ? open[open.length - 1] : document.body;
  };

  const inScope = (el) => {
    const root = scope();
    if (root !== document.body) return root.contains(el);
    const dialog = el.closest("dialog");
    return !dialog;
  };

  const clipsX = (style) => ["hidden", "clip", "auto", "scroll"].includes(style.overflowX);
  const clipsY = (style) => ["hidden", "clip", "auto", "scroll"].includes(style.overflowY);
  const userScrolls = (value) => value === "auto" || value === "scroll";

  // The part of a box that is actually on screen after every clipping ancestor.
  const visibleRect = (el, rect = el.getBoundingClientRect()) => {
    let left = Math.max(rect.left, 0);
    let top = Math.max(rect.top, 0);
    let right = Math.min(rect.right, innerWidth);
    let bottom = Math.min(rect.bottom, innerHeight);
    for (let node = el.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === "contents") continue;
      const box = node.getBoundingClientRect();
      if (clipsX(style)) {
        left = Math.max(left, box.left);
        right = Math.min(right, box.right);
      }
      if (clipsY(style)) {
        top = Math.max(top, box.top);
        bottom = Math.min(bottom, box.bottom);
      }
      if (style.position === "fixed") break;
    }
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  };

  const controls = () =>
    [...document.querySelectorAll(INTERACTIVE)].filter(
      (el) =>
        rendered(el) &&
        inScope(el) &&
        !el.closest(".sr-only") &&
        !(el.tagName === "INPUT" && el.type === "file" && el.closest("label")),
    );

  // Scroll a control into view the way a user can: only containers with
  // scrollbars may move. An overflow:hidden ancestor that has to move means a
  // pointer user cannot reach the control.
  const reach = (el) => {
    const problems = [];
    const hidden = [];
    for (let node = el.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      const x = node.scrollWidth > node.clientWidth + 1 && !userScrolls(style.overflowX);
      const y = node.scrollHeight > node.clientHeight + 1 && !userScrolls(style.overflowY);
      const root = node === document.documentElement || node === document.body;
      if ((x || y) && (root || style.overflowX !== "visible" || style.overflowY !== "visible"))
        hidden.push([node, node.scrollLeft, node.scrollTop]);
    }
    const page = document.scrollingElement;
    const pageStyle = getComputedStyle(document.documentElement);
    const bodyStyle = getComputedStyle(document.body);
    const pageScrolls =
      userScrolls(pageStyle.overflowY) ||
      (pageStyle.overflowY === "visible" && bodyStyle.overflowY !== "hidden");
    const pageScrollsX =
      userScrolls(pageStyle.overflowX) ||
      (pageStyle.overflowX === "visible" && bodyStyle.overflowX !== "hidden");
    const before = [page.scrollLeft, page.scrollTop];
    el.scrollIntoView({ block: "nearest", inline: "nearest" });
    for (const [node, left, top] of hidden)
      if (node.scrollLeft !== left || node.scrollTop !== top)
        problems.push(`${describe(el)} is only reachable by scrolling non-scrollable ${describe(node)}`);
    if (page.scrollTop !== before[1] && !pageScrolls)
      problems.push(`${describe(el)} is below a page that cannot scroll`);
    if (page.scrollLeft !== before[0] && !pageScrollsX)
      problems.push(`${describe(el)} is beside a page that cannot scroll`);
    return problems;
  };

  const covered = (el, x, y) => {
    const hit = document.elementFromPoint(x, y);
    if (!hit) return "nothing";
    if (el === hit || el.contains(hit)) return null;
    if (hit.tagName === "LABEL" && hit.control === el) return null;
    if (el.labels && [...el.labels].some((label) => label === hit || label.contains(hit)))
      return null;
    return describe(hit);
  };

  // Every control can be scrolled into view, is fully inside every clipping
  // ancestor and the window, and is not covered by anything else.
  const controlProblems = () => {
    const problems = [];
    for (const el of controls()) {
      problems.push(...reach(el));
      const rect = el.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) {
        problems.push(`${describe(el)} has no size`);
        continue;
      }
      const seen = visibleRect(el, rect);
      if (
        seen.width < Math.min(rect.width, innerWidth) - 1 ||
        seen.height < Math.min(rect.height, innerHeight) - 1
      )
        problems.push(
          `${describe(el)} is clipped: ${Math.round(seen.width)}x${Math.round(seen.height)} of ${Math.round(rect.width)}x${Math.round(rect.height)} visible`,
        );
      if (seen.width < 1 || seen.height < 1) continue;
      const cx = (seen.left + seen.right) / 2;
      const cy = (seen.top + seen.bottom) / 2;
      const hit = covered(el, cx, cy);
      if (hit) problems.push(`${describe(el)} is covered by ${hit}`);
    }
    return problems;
  };

  const textNodes = () => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) =>
        node.textContent.trim() &&
        node.parentElement &&
        rendered(node.parentElement) &&
        inScope(node.parentElement) &&
        !node.parentElement.closest(".sr-only, [aria-hidden='true'], option, script, style")
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT,
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  };

  const textRect = (node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    return range.getBoundingClientRect();
  };

  // Text that its own box or an ancestor cuts off without a way to scroll to it.
  const clippedText = ({ allowEllipsis = false } = {}) => {
    const problems = [];
    for (const node of textNodes()) {
      const parent = node.parentElement;
      const style = getComputedStyle(parent);
      if (allowEllipsis && style.textOverflow === "ellipsis" && parent.title) continue;
      let rect = textRect(node);
      if (rect.width < 1 || rect.height < 1) continue;
      for (let box = parent; box; box = box.parentElement) {
        const boxStyle = getComputedStyle(box);
        if (box === document.documentElement || box === document.body) continue;
        // Text inside a scrolling pane can be scrolled to; from here on the
        // pane itself is what must fit.
        if (userScrolls(boxStyle.overflowX) || userScrolls(boxStyle.overflowY)) {
          rect = box.getBoundingClientRect();
          continue;
        }
        const hiddenX = ["hidden", "clip"].includes(boxStyle.overflowX);
        const hiddenY = ["hidden", "clip"].includes(boxStyle.overflowY);
        if (!hiddenX && !hiddenY) continue;
        const outer = box.getBoundingClientRect();
        const cut =
          (hiddenX && (rect.left < outer.left - 1 || rect.right > outer.right + 1)) ||
          (hiddenY && (rect.top < outer.top - 1 || rect.bottom > outer.bottom + 1));
        if (cut) {
          problems.push(
            `text "${node.textContent.trim().slice(0, 40)}" in ${describe(parent)} is cut off by ${describe(box)}`,
          );
          break;
        }
      }
    }
    return problems;
  };

  const intersect = (a, b) =>
    Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) *
    Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top));

  // Visible text and controls must not sit on top of each other.
  const overlaps = () => {
    const boxes = [];
    for (const el of controls()) {
      const rect = visibleRect(el);
      if (rect.width > 0 && rect.height > 0) boxes.push({ el, rect, label: describe(el) });
    }
    for (const node of textNodes()) {
      const owner = node.parentElement;
      if (boxes.some((box) => box.el.contains(owner))) continue;
      for (const line of (() => {
        const range = document.createRange();
        range.selectNodeContents(node);
        return [...range.getClientRects()];
      })()) {
        const rect = visibleRect(owner, line);
        if (rect.width > 0 && rect.height > 0)
          boxes.push({
            el: owner,
            rect,
            label: `text "${node.textContent.trim().slice(0, 30)}"`,
          });
      }
    }
    const problems = [];
    for (let i = 0; i < boxes.length; i++)
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i];
        const b = boxes[j];
        if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el)) continue;
        if (intersect(a.rect, b.rect) > 4) problems.push(`${a.label} overlaps ${b.label}`);
      }
    return problems;
  };

  // Horizontal scrolling of the page or of a pane, except for code blocks,
  // which WCAG reflow treats as two-dimensional content.
  const horizontalScroll = () => {
    const problems = [];
    const root = document.scrollingElement;
    if (root.scrollWidth > innerWidth + 1)
      problems.push(`page is ${root.scrollWidth}px wide in a ${innerWidth}px window`);
    for (const el of document.querySelectorAll("*")) {
      if (!rendered(el) || !inScope(el) || el.closest("pre")) continue;
      const style = getComputedStyle(el);
      if (userScrolls(style.overflowX) && el.scrollWidth > el.clientWidth + 1)
        problems.push(`${describe(el)} scrolls sideways (${el.scrollWidth} > ${el.clientWidth})`);
    }
    return problems;
  };

  const seconds = (value) =>
    Math.max(...value.split(",").map((part) => (part.trim().endsWith("ms") ? parseFloat(part) / 1000 : parseFloat(part))));

  const motion = () => {
    const problems = [];
    for (const animation of document.getAnimations()) {
      const timing = animation.effect && animation.effect.getComputedTiming();
      if (animation.playState === "running" && timing && timing.duration > 0)
        problems.push(
          `animation ${animation.animationName || animation.constructor.name} runs on ${describe(animation.effect.target)}`,
        );
    }
    for (const el of document.querySelectorAll("*")) {
      const style = getComputedStyle(el);
      if (seconds(style.transitionDuration) > 0.01)
        problems.push(`${describe(el)} transitions for ${style.transitionDuration}`);
      if (style.scrollBehavior === "smooth") problems.push(`${describe(el)} scrolls smoothly`);
    }
    return [...new Set(problems)];
  };

  const focusInfo = () => {
    const el = document.activeElement;
    if (!el || el === document.body) return null;
    const rect = el.getBoundingClientRect();
    const seen = visibleRect(el, rect);
    const cx = (seen.left + seen.right) / 2;
    const cy = (seen.top + seen.bottom) / 2;
    return {
      name: describe(el),
      focusVisible: el.matches(":focus-visible"),
      rect: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
      visible: { width: seen.width, height: seen.height },
      coveredBy: seen.width > 0 && seen.height > 0 ? covered(el, cx, cy) : "nothing",
    };
  };

  return {
    controls: () => controls().map(describe),
    controlProblems,
    clippedText,
    overlaps,
    horizontalScroll,
    motion,
    focusInfo,
  };
})();
