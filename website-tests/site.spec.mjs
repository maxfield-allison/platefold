import { test, expect } from "@playwright/test";
const product = "platefold";
const display = "Plate|Fold";
const domain = "platefold.page";
test("sample page controls show real output pages and announce the selection", async ({
  page,
}) => {
  await page.goto("/");
  const controls = page.getByRole("group", { name: "Choose a sample page" });
  await controls.getByRole("button", { name: "Custom", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#sample-caption")).toHaveText(
    "Custom · A page cut to this diagram’s proportions.",
  );
  await expect(page.locator("#sample-image")).toHaveAttribute(
    "alt",
    "Custom ultra-wide page from the sample PDF containing the widest diagram",
  );
  await page.waitForFunction(() => {
    const image = document.querySelector("#sample-image");
    return image.complete && image.naturalWidth > image.naturalHeight * 5;
  });
  await expect(
    controls.getByRole("button", { name: "Custom", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    controls.getByRole("button", { name: "Letter", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
});
test("interface appearance can be changed with the keyboard", async ({
  page,
}) => {
  await page.goto("/");
  const light = page.getByRole("button", { name: "Light", exact: true });
  await light.focus();
  await page.keyboard.press("Space");
  await expect(light).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#interface-image")).toHaveAttribute(
    "alt",
    "PlateFold’s light interface with an example document and export settings",
  );
  await page.waitForFunction(
    () =>
      document.querySelector("#interface-image").complete &&
      document.querySelector("#interface-image").naturalWidth === 2160,
  );
});
test("without JavaScript the homepage keeps the sample, workspace, qualification notice and installation links", async ({
  browser,
}) => {
  const context = await browser.newContext({
    javaScriptEnabled: false,
    viewport: { width: 320, height: 900 },
  });
  try {
    const page = await context.newPage();
    await page.goto("http://127.0.0.1:38113/");
    await expect(page.locator("#sample-image")).toBeVisible();
    await expect(page.locator("[data-page-controls]")).toBeHidden();
    await expect(page.locator("[data-interface-controls]")).toBeHidden();
    await expect(page.locator(".notice")).toContainText(
      "Desktop downloads are undergoing final release qualification.",
    );
    await expect(
      page.getByRole("link", { name: "Get started in the docs" }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  } finally {
    await context.close();
  }
});
for (const width of [1280,390,320]) {
  test(`home, pricing, privacy and real links at ${width}px`, async ({page}) => {
    await page.setViewportSize({width,height:900});
    const errors=[], failures=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('response',r=>{if(r.status()>=400)failures.push(r.status()+' '+r.url());});
    await page.goto('/');
    await expect(page.getByRole('heading',{level:1})).toBeVisible();
    await expect(page.locator('header a[href="/pricing/"]')).toBeVisible();
    await expect(page.getByRole('link',{name:'Support the projects',exact:true})).toHaveAttribute('href','/pricing/#support-title');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await expect(page.getByRole('link',{name:'Get started in the docs'})).toHaveAttribute('href',product==='platefold'?'https://maxfield-allison.github.io/platefold/get-started/':'https://maxfield-allison.github.io/dnsweaver/getting-started/');
    await page.screenshot({path:`test-results/home-${width}.png`,fullPage:true});
    await page.locator('header a[href="/pricing/"]').click();
    await expect(page.locator('.stage.is-live')).toBeVisible({timeout:100000});
    await expect(page).toHaveTitle(display+' · Pricing');
    await expect(page.locator('link[rel="canonical"]')).toHaveAttribute('href',`https://${domain}/pricing/`);
    expect(await page.evaluate(()=>window.rivetStage.state().mode)).toBe('still');
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    await expect(page.locator('.plan .price')).toHaveCount(3);
    expect((await page.locator('.plan .price').allTextContents()).every(t=>t.startsWith('$0'))).toBe(true);
    await expect(page.getByRole('link',{name:'Get '+display,exact:true})).toHaveAttribute('href','/#get-started');
    await expect(page.getByRole('link',{name:'Sponsor on GitHub'})).toHaveAttribute('href',`https://github.com/sponsors/maxfield-allison?metadata_campaign=${product}-pricing`);
    await page.screenshot({path:`test-results/pricing-${width}.png`,fullPage:true});
    await page.getByRole('link',{name:'Privacy',exact:true}).click();
    await expect(page.getByRole('heading',{name:'Privacy',exact:true})).toBeVisible();
    expect(errors).toEqual([]);expect(failures).toEqual([]);
  });
}
test('normal motion plays the full scene into idle', async ({page})=>{
  await page.emulateMedia({reducedMotion:'no-preference'});
  await page.goto('/pricing/');
  await expect(page.locator('.stage.is-live')).toBeVisible({timeout:100000});
  await page.waitForFunction(()=>window.rivetStage.state().mode==='idle',null,{timeout:90000});
  expect(await page.evaluate(()=>window.rivetStage.state().t)).toBe(7);
});
test('keyboard can start, pause, resume and skip the story', async ({page})=>{
  await page.goto('/pricing/');
  await expect(page.locator('.stage.is-live')).toBeVisible({timeout:100000});
  await page.locator('[data-action="replay"]').focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>window.rivetStage.state().t>.1);
  await page.locator('[data-action="pause"]').focus();await page.keyboard.press('Space');
  const paused=await page.evaluate(()=>window.rivetStage.state().t);
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  expect(await page.evaluate(()=>window.rivetStage.state().t)).toBe(paused);
  await page.keyboard.press('Space');
  await page.waitForFunction(t=>window.rivetStage.state().t>t,paused);
  await page.locator('[data-action="skip"]').focus();await page.keyboard.press('Enter');
  expect(await page.evaluate(()=>window.rivetStage.state().mode)).toBe('still');
});
test('missing artwork leaves the free offer and poster usable', async ({page})=>{
  await page.route('**/pricing/art/manifest.json',r=>r.fulfill({status:503,body:'Unavailable'}));
  await page.goto('/pricing/');
  await expect(page.locator('[data-stage]')).toHaveAttribute('data-failed','true',{timeout:100000});
  await expect(page.locator('.stage__poster img')).toBeVisible();
  await expect(page.locator('.plan .price')).toHaveCount(3);
  await expect(page.getByRole('link',{name:'Get '+display,exact:true})).toBeVisible();
});
test('no JavaScript keeps the poster, free offer and links', async ({browser})=>{
  const context=await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:900}});
  try{const page=await context.newPage();await page.goto('http://127.0.0.1:38113/pricing/');
    await expect(page.locator('.stage__poster img')).toBeVisible();
    await expect(page.locator('.plan .price')).toHaveCount(3);
    await expect(page.getByRole('link',{name:'Sponsor on GitHub'})).toBeVisible();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  }finally{await context.close();}
});
