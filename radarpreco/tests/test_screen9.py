from pathlib import Path
from playwright.sync_api import sync_playwright
p=Path(__file__).resolve().parents[1]
html=(p/'src/index.html').read_text(encoding='utf8')
with sync_playwright() as play:
 browser=play.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox','--no-proxy-server'])
 for kind,width,height in [('desktop',1440,900),('mobile',390,844)]:
  page=browser.new_page(viewport={'width':width,'height':height})
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.set_content(html,wait_until='domcontentloaded')
  page.wait_for_timeout(800)
  assert page.locator('#accountBtn').count()==1
  assert page.locator('#highlights').count()==1
  assert page.locator('.r9-bottom').count()==1
  page.locator('#accountBtn').click()
  assert page.locator('#accountDialog').is_visible()
  page.locator('#accountClose').click()
  assert not page.locator('#accountDialog').is_visible()
  if kind=='mobile':
   assert page.locator('.r9-bottom').is_visible()
  assert not errors,errors
  page.screenshot(path=str(p/f'radar9_{kind}.png'),full_page=True)
  print('PASS '+kind)
  page.close()
 browser.close()
