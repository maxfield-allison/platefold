const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
if (!reducedMotion.matches && "IntersectionObserver" in window) {
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        entry.target.classList.replace("reveal-pending", "reveal-visible");
        observer.unobserve(entry.target);
      }
    },
    { threshold: 0.08 },
  );
  for (const element of document.querySelectorAll("[data-reveal]")) {
    if (element.getBoundingClientRect().top < innerHeight) continue;
    element.classList.add("reveal-pending");
    observer.observe(element);
  }
}

const samples = {
  letter: {
    src: "/assets/pdf-letter.jpg",
    alt: "Portrait Letter page from the PlateFold sample PDF, with body text and an inline diagram",
    caption: "Letter · Body text and a diagram that fits inline.",
  },
  landscape: {
    src: "/assets/pdf-landscape.jpg",
    alt: "Landscape Letter page from the sample PDF containing a wider diagram",
    caption: "Landscape Letter · The next diagram gets a wider page.",
  },
  a3: {
    src: "/assets/pdf-a3.jpg",
    alt: "A3 landscape page from the sample PDF containing a larger diagram",
    caption: "A3 landscape · A larger sheet for a larger figure.",
  },
  custom: {
    src: "/assets/pdf-custom.jpg",
    alt: "Custom ultra-wide page from the sample PDF containing the widest diagram",
    caption: "Custom · A page cut to this diagram’s proportions.",
  },
};
const sampleImage = document.querySelector("#sample-image");
const sampleCaption = document.querySelector("#sample-caption");
const sampleStage = document.querySelector(".paper-stage");
const pageControls = document.querySelector("[data-page-controls]");
pageControls.hidden = false;
for (const button of pageControls.querySelectorAll("button")) {
  button.addEventListener("click", () => {
    const sample = samples[button.dataset.page];
    sampleImage.src = sample.src;
    sampleImage.alt = sample.alt;
    sampleCaption.textContent = sample.caption;
    for (const choice of pageControls.querySelectorAll("button"))
      choice.setAttribute("aria-pressed", String(choice === button));
    sampleStage.classList.remove("is-changing");
    if (!reducedMotion.matches)
      requestAnimationFrame(() => sampleStage.classList.add("is-changing"));
  });
}
const interfaceControls = document.querySelector("[data-interface-controls]");
interfaceControls.hidden = false;
for (const button of interfaceControls.querySelectorAll("button")) {
  button.addEventListener("click", () => {
    const dark = button.dataset.interface === "dark";
    document.querySelector("#interface-image").src = dark
      ? "/assets/platefold-workspace-dark.jpg"
      : "/assets/platefold-workspace.jpg";
    document.querySelector("#interface-image").alt =
      `PlateFold’s ${dark ? "dark" : "light"} interface with an example document and export settings`;
    for (const choice of interfaceControls.querySelectorAll("button"))
      choice.setAttribute("aria-pressed", String(choice === button));
  });
}
