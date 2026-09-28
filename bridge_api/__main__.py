"""``python -m bridge_api`` — launch the ops API under uvicorn.

Bind address: 172.17.0.1:8765 (the host's docker0 gateway IP). The only
production caller is Caddy via ``reverse_proxy 172.17.0.1:8765`` from
the portfolio_internal network. Binding here (instead of 0.0.0.0)
makes the EC2 public IP unable to reach the socket at all, so the
external perimeter no longer depends on UFW. Host-side callers that
were using 127.0.0.1 must switch to 172.17.0.1.
"""

import uvicorn


def main() -> None:
    uvicorn.run("bridge_api.app:app", host="172.17.0.1", port=8765)


if __name__ == "__main__":
    main()
