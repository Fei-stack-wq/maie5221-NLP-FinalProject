"""Serve the actual Studio preview route with a local slide project and no provider."""
from pathlib import Path
from tempfile import TemporaryDirectory

from miniagent.config import Settings
from miniagent.studio_server import create_server
from miniagent.studio_service import StudioService


def main():
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        project = root / "slides/agents/analyst"
        project.mkdir(parents=True)
        (project / "deck.html").write_text("""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<link rel="stylesheet" href="deck.css"><main><section>研究范围</section><section hidden>投资风险</section></main>
<button id="back">上一页</button><button id="next">下一页</button>
<a href="about:blank" target="_top">打开链接</a><script src="deck.js"></script></html>""",
                                            encoding="utf-8")
        (project / "deck.css").write_text("section { color: rgb(10, 20, 30) }")
        (project / "deck.js").write_text("""
const slides = [...document.querySelectorAll('section')];
function show(i) { slides.forEach((slide, n) => slide.hidden = i !== n); }
document.querySelector('#next').onclick = () => show(1);
document.querySelector('#back').onclick = () => show(0);
""")
        service = StudioService(root, Settings())
        server = create_server(service, port=0)
        print(f"http://127.0.0.1:{server.server_port}/api/runs/slides/preview?path=agents/analyst/deck.html",
              flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            service.close()


if __name__ == "__main__":
    main()
