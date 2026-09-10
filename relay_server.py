"""
ComfyUI Mobile Relay
====================

A minimal relay server that lets you run ComfyUI from your phone: pick a saved
workflow, set the batch count, and hit Run. Generated images can be browsed
through a simple gallery.

ComfyUI itself is never exposed to the network -- only this relay server is,
and it is meant to be reachable exclusively over a private network (Tailscale).

ComfyUI本体はネットワークに公開せず、この中継サーバーだけをプライベート
ネットワーク(Tailscale)経由で公開する構成を前提としています。

Requirements / 事前準備:
  pip install fastapi uvicorn playwright pillow
  playwright install chromium

Run / 起動:
  uvicorn relay_server:app --host 0.0.0.0 --port 8080

Debug run with a visible browser window / ブラウザ画面を表示して動作を確認する:
  $env:HEADLESS="0"
  uvicorn relay_server:app --host 127.0.0.1 --port 8080

Setup / セットアップ:
  Save the workflows you want to run under the "mobile" folder, in the normal
  format (NOT the API format). In Graph mode, use Save As with a name like
  "mobile/xxx" to place it there.
  スマホから実行したいワークフローは「mobile」フォルダに通常形式(API形式では
  ない)で保存する。Save As で「mobile/xxx」のように指定すればよい。

How a workflow is loaded / ワークフローの読み込み方:
  We do NOT click through the Workflows sidebar. Instead the relay reads the
  .json straight from disk and hands it to ComfyUI's own loader:

      window.app.loadGraphData(graphData)

  This is the same function the sidebar ends up calling, so everything the
  frontend normally does (widgets, custom nodes, seed and wildcard handling)
  still happens. Going through the API instead of the DOM avoids a long list
  of problems: sidebar open/closed state, folder expand/collapse, virtualized
  scrolling, the search box, name collisions between the tab bar and the tree,
  and the fact that the sidebar's workflow list is only fetched once per page
  load (so freshly saved workflows were invisible until a reload).
  サイドバーをクリックする方式はやめ、中継サーバーがディスクから読んだJSONを
  ComfyUI自身のローダーに直接渡す。サイドバーが内部で呼んでいるのと同じ関数な
  ので、フロントエンドの処理(ウィジェット、カスタムノード、seedやwildcardの
  扱い)は通常どおり動く。DOMを経由しないことで、サイドバーの開閉・フォルダ展開・
  仮想スクロール・検索ボックス・タブバーとの名前衝突・一覧がページ読み込み時
  にしか取得されない問題を、まとめて回避できる。

Why the same workflow is not reloaded / 同じワークフローを読み直さない理由:
  Reloading from disk resets every widget to its saved value, including seeds.
  With control_after_generate set to increment or fixed, that would make every
  run produce the same result. So when the requested workflow is already the
  active one we keep it as is, which preserves the frontend's seed progression
  exactly as it behaves for a person clicking Run repeatedly.
  ディスクから読み直すとseedを含む全ウィジェットが保存時の値に戻る。
  control_after_generateがincrement/fixedだと毎回同じ結果になってしまうため、
  既に開いているワークフローはそのまま使い、人が繰り返しRunを押したときと同じ
  seedの進み方を保つ。
  Switching to a different workflow always reads from disk, so a stale copy
  only matters when you edit the workflow that is currently active. The
  "Reload WF" button (POST /reload) covers that case.
  別のワークフローに切り替えれば必ずディスクから読み直すため、古いままになるの
  は「今開いているワークフローをPC側で編集した」場合だけ。その時は画面の
  「Reload WF」ボタン(POST /reload)を使う。

Navigation pitfalls worth remembering / ナビゲーション周りの注意点:
  - "networkidle" never settles while generating, because ComfyUI streams
    progress over a WebSocket. Navigation uses "domcontentloaded".
    生成中はWebSocketで通信が続くためnetworkidleが成立しない。
  - beforeunload fires when the workflow has unsaved changes. accept() means
    "leave the page"; dismissing it would cancel a reload and hang.
    未保存変更があるとbeforeunloadが出る。accept()が「離脱」の意味。
  - domcontentloaded fires long before the Vue UI is usable, so we wait for a
    real element instead of sleeping a fixed amount.
    domcontentloadedはUIが使える状態より前に発火するので、要素の出現を待つ。

Remaining DOM dependency / 残っているDOM依存:
  Only set_batch_count and click_queue touch the DOM now.
  DOMを操作するのは set_batch_count と click_queue だけになった。
"""

import asyncio
import io
import json
import os
import urllib.request
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from PIL import Image
from playwright.async_api import Browser, Page, async_playwright

# ---------------------------------------------------------------------------
# Configuration (overridable via environment variables)
# 設定 (環境変数で上書き可能)
# ---------------------------------------------------------------------------

COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
COMFYUI_ROOT = Path(
    os.environ.get("COMFYUI_ROOT", r"C:\ComfyUI_windows_portable\ComfyUI")
)
OUTPUT_DIR = Path(os.environ.get("COMFYUI_OUTPUT_DIR", str(COMFYUI_ROOT / "output")))

# Folder holding the workflows offered in the dropdown.
# ドロップダウンに出すワークフローを置くフォルダ
MOBILE_FOLDER = os.environ.get("MOBILE_FOLDER", "mobile")
WORKFLOW_DIR = Path(
    os.environ.get(
        "COMFYUI_WORKFLOW_DIR",
        str(COMFYUI_ROOT / "user" / "default" / "workflows" / MOBILE_FOLDER),
    )
)

# Selectors for the two controls we still drive through the DOM.
# DOM経由で操作する2つのコントロールのセレクタ
QUEUE_BUTTON = '[data-testid="queue-button"]'
BATCH_COUNT_NAME = "Batch Count"

# Set HEADLESS=0 to watch the automated browser / デバッグ用にブラウザを表示
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
# Default timeout for individual UI actions (ms) / 個々のUI操作のタイムアウト
ACTION_TIMEOUT_MS = int(os.environ.get("ACTION_TIMEOUT_MS", "8000"))
# Page navigation needs a longer budget than a single click.
# ページ遷移はクリック1回より時間がかかるので別枠で長めに取る
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "30000"))
# Building the Vue UI is slow, especially during generation.
# 生成中はUIの構築がさらに遅くなるので長めに待てるようにする
UI_READY_TIMEOUT_MS = int(os.environ.get("UI_READY_TIMEOUT_MS", "45000"))
# Reload the workflow from disk on every run / 毎回ディスクから読み直す
REOPEN_SAME_WORKFLOW = os.environ.get("REOPEN_SAME_WORKFLOW", "0") == "1"
VIEWPORT = {
    "width": int(os.environ.get("VIEWPORT_WIDTH", "1600")),
    "height": int(os.environ.get("VIEWPORT_HEIGHT", "1400")),
}
THUMB_SIZE = (400, 400)
GALLERY_LIMIT = 200
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

app = FastAPI(title="ComfyUI Mobile Relay")

# ---------------------------------------------------------------------------
# Headless browser management / ヘッドレスブラウザの管理
# ---------------------------------------------------------------------------

_playwright = None
_browser: Browser | None = None
_page: Page | None = None
_lock = asyncio.Lock()
# Which workflow the headless page currently holds / 今読み込んでいるワークフロー
_current_workflow: str | None = None


def _handle_dialog(dialog) -> None:
    """Answer native browser dialogs so navigation is never blocked.

    For beforeunload, accept() means "leave the page"; dismissing it would
    cancel a reload and hang until it times out.

    ネイティブダイアログに応答してナビゲーションが止まらないようにする。
    beforeunloadはaccept()が「離脱」を意味する。
    """
    if dialog.type == "beforeunload":
        asyncio.ensure_future(dialog.accept())
    else:
        asyncio.ensure_future(dialog.dismiss())


async def _wait_ui_ready(page: Page) -> None:
    """Wait until ComfyUI's app object and toolbar are actually usable.

    ComfyUIのappオブジェクトとツールバーが操作可能になるまで待つ。
    """
    await page.wait_for_function(
        "() => typeof window.app?.loadGraphData === 'function'",
        timeout=UI_READY_TIMEOUT_MS,
    )
    await page.locator(QUEUE_BUTTON).first.wait_for(
        state="visible", timeout=UI_READY_TIMEOUT_MS
    )
    # Small settle so freshly mounted handlers are attached.
    # マウント直後のハンドラが繋がるまで少しだけ待つ
    await page.wait_for_timeout(800)


async def ensure_browser() -> Page:
    """Start the headless browser if needed and return the ComfyUI page.

    ヘッドレスブラウザが起動していなければ起動し、ComfyUIを開いたPageを返す。
    """
    global _playwright, _browser, _page, _current_workflow

    if _page is not None and not _page.is_closed():
        return _page

    async with _lock:
        if _page is not None and not _page.is_closed():
            return _page

        if _playwright is None:
            _playwright = await async_playwright().start()

        _browser = await _playwright.chromium.launch(
            headless=HEADLESS,
            slow_mo=0 if HEADLESS else 800,
        )
        context = await _browser.new_context(viewport=VIEWPORT)
        # Keep failures fast so the phone gets an error instead of hanging.
        # 失敗を早く返すことで、スマホ側が待たされ続けるのを防ぐ
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        _page = await context.new_page()
        _page.on("dialog", _handle_dialog)
        # NOTE: domcontentloaded, not networkidle -- see module docstring.
        await _page.goto(COMFYUI_URL, wait_until="domcontentloaded")
        await _wait_ui_ready(_page)
        _current_workflow = None

    return _page


async def reload_page(page: Page) -> None:
    """Reload ComfyUI from scratch. / ComfyUIを再読み込みする。"""
    global _current_workflow
    await page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await _wait_ui_ready(page)
    _current_workflow = None


# ---------------------------------------------------------------------------
# Workflow handling / ワークフローの取り扱い
# ---------------------------------------------------------------------------


def list_workflows() -> list[str]:
    """Return workflow names (without extension) stored in the mobile folder.

    mobileフォルダに保存されているワークフロー名(拡張子なし)を返す。
    """
    if not WORKFLOW_DIR.exists():
        return []
    return sorted(p.stem for p in WORKFLOW_DIR.glob("*.json"))


def read_workflow(name: str) -> dict:
    """Read a workflow JSON from the mobile folder.

    mobileフォルダからワークフローのJSONを読む。
    """
    path = WORKFLOW_DIR / f"{name}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Workflow not found: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=422, detail=f"Workflow '{name}' is not valid JSON: {e}"
        ) from e


async def load_workflow(page: Page, name: str) -> dict:
    """Load the workflow into ComfyUI via its own loader.

    Skips the load when the workflow is already active, to preserve seed
    progression (see module docstring). Returns a small status dict.

    ComfyUI自身のローダーでワークフローを読み込む。既に開いている場合はseedの
    進行を保つため読み込みを省略する。
    """
    global _current_workflow

    if not REOPEN_SAME_WORKFLOW and _current_workflow == name:
        node_count = await page.evaluate(
            "() => window.app?.graph?._nodes?.length ?? -1"
        )
        # Guard against the page having been reloaded behind our back.
        # 何らかの理由でページが差し替わっていた場合に備える
        if node_count > 0:
            return {"loaded": False, "reused": True, "nodes": node_count}

    data = read_workflow(name)

    result = await page.evaluate(
        """async (graph) => {
            try {
                await window.app.loadGraphData(graph);
                return { ok: true, nodes: window.app?.graph?._nodes?.length ?? -1 };
            } catch (e) {
                return { ok: false, error: String(e) };
            }
        }""",
        data,
    )

    if not result.get("ok"):
        _current_workflow = None
        raise HTTPException(
            status_code=409,
            detail=f"Failed to load workflow '{name}': {result.get('error')}",
        )

    expected = len(data.get("nodes", []))
    loaded = result.get("nodes", -1)
    if loaded <= 0:
        _current_workflow = None
        raise HTTPException(
            status_code=409,
            detail=f"Workflow '{name}' loaded but the graph is empty",
        )

    _current_workflow = name
    # Let widgets finish mounting before we touch the toolbar.
    # ツールバーを操作する前にウィジェットの構築を待つ
    await page.wait_for_timeout(1200)
    return {"loaded": True, "reused": False, "nodes": loaded, "expected": expected}


async def set_batch_count(page: Page, count: int) -> None:
    """Set the Batch Count field next to the Queue button.

    Note: [aria-label="Batch Count"] matches both the wrapper div and the input,
    which triggers a strict mode violation -- so target the input by role.
    aria-labelはラッパーのdivとinputの両方に付いているため、roleでinputを狙う。
    """
    batch_input = page.get_by_role("textbox", name=BATCH_COUNT_NAME)
    await batch_input.wait_for(state="visible")
    await batch_input.fill(str(count))
    # Some frameworks commit the value on blur/change. / Tabで値を確定させる
    await batch_input.press("Tab")


async def click_queue(page: Page) -> None:
    """Press the Queue (Run) button. / Queueボタンを押す。"""
    queue_btn = page.locator(QUEUE_BUTTON).first
    await queue_btn.wait_for(state="visible")
    await queue_btn.click()


def _fetch_queue() -> dict:
    """Fetch ComfyUI's /queue. / ComfyUI本体の /queue を取得する。"""
    with urllib.request.urlopen(f"{COMFYUI_URL}/queue", timeout=5) as r:
        return json.load(r)


def _queue_len() -> int:
    """Return how many prompts are queued right now (-1 on failure).

    現在キューに積まれている数を返す(取得失敗時は -1)。
    """
    try:
        d = _fetch_queue()
        return len(d.get("queue_running", [])) + len(d.get("queue_pending", []))
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Run / status endpoints / 実行トリガー・状態取得
# ---------------------------------------------------------------------------


@app.get("/workflows")
async def workflows():
    """Workflow names offered in the dropdown. / ドロップダウンの候補一覧。"""
    return {"folder": MOBILE_FOLDER, "workflows": list_workflows()}


@app.post("/run")
async def run(
    count: int = Query(..., ge=1, le=100),
    workflow: str = Query(...),
):
    # Only accept names present in the folder, so nothing outside can be opened.
    # フォルダ外を指定されないよう、一覧にある名前だけ受け付ける
    if workflow not in list_workflows():
        raise HTTPException(status_code=400, detail=f"Unknown workflow: {workflow}")

    page = await ensure_browser()
    async with _lock:
        before = _queue_len()
        load_info = await load_workflow(page, workflow)
        await set_batch_count(page, count)
        # Read the value back to verify it reached the UI. / 反映を読み戻して確認
        actual = await page.get_by_role(
            "textbox", name=BATCH_COUNT_NAME
        ).input_value()
        await click_queue(page)
        await page.wait_for_timeout(2000)
        after = _queue_len()
    return {
        "workflow": workflow,
        "load": load_info,
        "batch_count_in_ui": actual,
        "queue_before": before,
        "queue_after": after,
        "queued": after > before,
    }


@app.get("/status")
async def status():
    """ComfyUI queue status. / ComfyUIのキュー状況(実行中/待機)。"""
    try:
        d = _fetch_queue()
        running = len(d.get("queue_running", []))
        pending = len(d.get("queue_pending", []))
        return {"running": running, "pending": pending, "total": running + pending}
    except Exception as e:
        return {"error": str(e)}


@app.post("/reload")
async def reload_endpoint():
    """Force the next run to read the workflow from disk again.

    Use this after editing on the PC the workflow that is currently active.
    It also reloads the ComfyUI page itself, which is a good recovery step if
    the UI ever gets stuck.

    今開いているワークフローをPC側で編集した後、その内容を反映させたい時に使う。
    ページ自体も再読み込みするので、UIがおかしくなった時の復旧手段にもなる。
    """
    page = await ensure_browser()
    async with _lock:
        await reload_page(page)
    return {"status": "reloaded"}


@app.post("/stop")
async def stop():
    """Shut down the headless browser (restarted automatically on next /run).

    ヘッドレスブラウザを終了する(次回 /run 時に自動で再起動する)。
    """
    global _browser, _page, _playwright, _current_workflow
    async with _lock:
        if _browser is not None:
            await _browser.close()
            _browser = None
            _page = None
        if _playwright is not None:
            await _playwright.stop()
            _playwright = None
        _current_workflow = None
    return {"status": "stopped"}


@app.get("/debug/screenshot")
async def debug_screenshot():
    """Screenshot of what the headless browser currently shows.

    ヘッドレスブラウザが今表示している画面のキャプチャ。
    """
    page = await ensure_browser()
    buf = await page.screenshot(full_page=False)
    return StreamingResponse(io.BytesIO(buf), media_type="image/png")


@app.get("/debug/state")
async def debug_state():
    """What the headless browser currently holds.

    ヘッドレスブラウザが今保持している状態。
    """
    page = await ensure_browser()
    nodes = await page.evaluate("() => window.app?.graph?._nodes?.length ?? -1")
    return {
        "current_workflow": _current_workflow,
        "title": await page.title(),
        "nodes": nodes,
    }


# ---------------------------------------------------------------------------
# Gallery (view only; saving is up to the user on their device)
# ギャラリー(閲覧専用。保存は利用者が端末側で明示的に行う)
# ---------------------------------------------------------------------------


def _safe_path(rel: str) -> Path:
    """Resolve a path while preventing traversal outside OUTPUT_DIR.

    OUTPUT_DIR外へのパストラバーサルを防ぎつつ絶対パスに解決する。
    """
    base = OUTPUT_DIR.resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(status_code=400, detail="Invalid path")
    return target


def _list_images(limit: int = GALLERY_LIMIT) -> list[Path]:
    """Images under the output folder, newest first, subfolders included.

    出力フォルダ配下の画像を更新日時の新しい順に返す(サブフォルダ含む)。
    """
    if not OUTPUT_DIR.exists():
        return []
    files = [f for f in OUTPUT_DIR.rglob("*") if f.suffix.lower() in IMAGE_EXTS]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:limit]


@app.get("/gallery", response_class=HTMLResponse)
async def gallery():
    files = _list_images()
    items_html = "".join(
        f'<a href="/full/{f.relative_to(OUTPUT_DIR).as_posix()}" target="_blank">'
        f'<img src="/thumb/{f.relative_to(OUTPUT_DIR).as_posix()}" loading="lazy"></a>'
        for f in files
    )
    html = f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gallery</title>
<style>
  body {{ margin:0; background:#111; }}
  .bar {{ padding:8px; color:#eee; font-family:sans-serif; font-size:14px; }}
  .bar a {{ color:#7af; }}
  .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(140px,1fr)); gap:4px; padding:4px; }}
  .grid img {{ width:100%; height:140px; object-fit:cover; display:block; background:#222; }}
</style></head>
<body>
  <div class="bar"><a href="/">&larr; Back to run</a></div>
  <div class="grid">{items_html}</div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/thumb/{rel_path:path}")
async def thumb(rel_path: str):
    p = _safe_path(rel_path)
    if not p.is_file():
        raise HTTPException(status_code=404)
    img = Image.open(p)
    img.thumbnail(THUMB_SIZE)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=80)
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/jpeg")


@app.get("/full/{rel_path:path}")
async def full(rel_path: str):
    p = _safe_path(rel_path)
    if not p.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(p)


# ---------------------------------------------------------------------------
# Mobile UI (workflow picker + batch count + run + queue status)
# スマホ用の操作画面
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        """<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ComfyUI Remote</title>
<style>
  body{font-family:sans-serif;background:#111;color:#eee;display:flex;flex-direction:column;
       align-items:center;padding:32px 16px;gap:14px;}
  h2{margin:4px 0;font-size:18px;font-weight:normal;color:#aaa;}
  select{font-size:18px;padding:10px;border-radius:8px;border:none;max-width:90vw;
         background:#222;color:#eee;}
  input{font-size:24px;width:80px;text-align:center;padding:8px;border-radius:8px;border:none;}
  button{font-size:20px;padding:12px 32px;border-radius:8px;border:none;background:#4a7;color:#fff;}
  button:disabled{background:#555;}
  #reload{font-size:15px;padding:8px 18px;background:#446;}
  a{color:#7af;margin-top:8px;}
  #status{min-height:1.5em;text-align:center;max-width:90vw;}
  #queue{min-height:1.5em;color:#9c9;font-size:18px;}
</style></head>
<body>
  <h2>Workflow</h2>
  <select id="workflow"><option>Loading...</option></select>
  <h2>Batch Count</h2>
  <input id="count" type="number" value="1" min="1" max="100">
  <button id="run" disabled>Run</button>
  <button id="reload">Reload WF</button>
  <div id="status"></div>
  <div id="queue"></div>
  <a href="/gallery">Open gallery</a>
<script>
  const btn = document.getElementById('run');
  const reloadBtn = document.getElementById('reload');
  const status = document.getElementById('status');
  const queueEl = document.getElementById('queue');
  const wfSelect = document.getElementById('workflow');

  async function loadWorkflows() {
    try {
      const d = await (await fetch('/workflows')).json();
      wfSelect.innerHTML = '';
      if (!d.workflows.length) {
        wfSelect.innerHTML = '<option>(no workflows in "' + d.folder + '")</option>';
        return;
      }
      for (const w of d.workflows) {
        const o = document.createElement('option');
        o.value = w; o.textContent = w;
        wfSelect.appendChild(o);
      }
      btn.disabled = false;
    } catch (e) {
      wfSelect.innerHTML = '<option>(failed to load)</option>';
    }
  }

  btn.onclick = async () => {
    const count = document.getElementById('count').value;
    const wf = wfSelect.value;
    btn.disabled = true;
    status.textContent = 'Running...';
    try {
      const res = await fetch('/run?count=' + count + '&workflow=' + encodeURIComponent(wf),
                              {method: 'POST'});
      const d = await res.json();
      if (!res.ok) {
        status.textContent = 'Error: ' + (d.detail || res.status);
      } else if (d.queued) {
        status.textContent = 'Queued: ' + d.workflow + ' (batch ' + d.batch_count_in_ui + ')';
      } else {
        status.textContent = 'Queue did not grow (' + d.workflow
          + ' / batch ' + d.batch_count_in_ui + ' / ' + d.queue_before + '->' + d.queue_after + ')';
      }
    } catch (e) {
      status.textContent = 'Connection error';
    }
    btn.disabled = false;
    poll();
  };

  // Forces the next run to read the workflow from disk again.
  // Needed after editing on the PC the workflow that is currently loaded.
  reloadBtn.onclick = async () => {
    reloadBtn.disabled = true;
    btn.disabled = true;
    status.textContent = 'Reloading...';
    try {
      const res = await fetch('/reload', {method: 'POST'});
      status.textContent = res.ok ? 'Workflow list refreshed'
                                  : ('Error: ' + res.status);
    } catch (e) {
      status.textContent = 'Connection error';
    }
    reloadBtn.disabled = false;
    await loadWorkflows();
  };

  async function poll() {
    try {
      const d = await (await fetch('/status')).json();
      if (d.error) {
        queueEl.textContent = '';
      } else if (d.total === 0) {
        queueEl.textContent = 'Queue is empty';
      } else {
        queueEl.textContent = 'Running ' + d.running + ' / pending ' + d.pending;
      }
    } catch (e) {
      queueEl.textContent = '';
    }
  }

  loadWorkflows();
  poll();
  setInterval(poll, 3000);
</script>
</body></html>"""
    )
