#!/bin/zsh
# Reopen the fixed local Studio; --reuse applies code updates once the service is idle.
set -eu
cd -- "$(dirname -- "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '没有找到项目虚拟环境。请先在此目录运行：'
  print 'python3 -m venv .venv'
  print '.venv/bin/python -m pip install -r requirements.txt'
  read 'reply?按回车关闭…'
  exit 1
fi
exec .venv/bin/python -m miniagent studio --port 8765 --strict-port --reuse
