# -*- coding: utf-8 -*-
"""启动云端 API：py -m cloudapi.main [--config cloud_config.json] [--host ..] [--port ..]"""

import argparse
import logging

import uvicorn

from . import config as cfgmod
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="V3 云端 API")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    cfg = cfgmod.load(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = args.host or cfg["server"]["host"]
    port = args.port or int(cfg["server"]["port"])
    app = create_app(cfg)
    # ★★★ WebSocket 心跳必须放宽（2026-09-22）：
    #   uvicorn 默认 ws_ping_interval=20s / ws_ping_timeout=20s，意味着服务端
    #   20 秒收不到 pong 就主动发 1011 断开。而客户本地 Agent 执行某些报表 SQL
    #   可能要几十秒（历史上 product-ranking 在真库跑到 48~89s）——
    #   一旦超过 20s，Agent 会被判定 keepalive 超时踢掉，表现为
    #   「Agent 假死 + 前端 502 AGENT_OFFLINE」。
    #   ⇒ ping_interval 放宽到 60s、timeout 放宽到 120s，给慢查询留出余量。
    #     （Agent 侧已在 agent_daemon 里把 op 执行移到线程池，不再阻塞心跳；
    #       这里是链路层的第二道保险。）
    uvicorn.run(
        app, host=host, port=port, log_level="info",
        ws_ping_interval=60,
        ws_ping_timeout=120,
    )


if __name__ == "__main__":
    main()
