# Backspace 홈서버 배포와 모바일 개발

이 브랜치는 홈서버에서 사용하는 Backspace 1.11.0 소스를 기준으로 한다. 모바일 화면은 `packages/web`, 로그인과 LiveKit 토큰 API는 `packages/server`에서 개발한다.

## 모바일 화면 로컬 개발

Node.js 22.12 이상과 pnpm 10이 필요하다. 저장소 루트에서 실행한다.

```sh
pnpm install --frozen-lockfile
cp .env.example .env
# .env의 JWT_SECRET에 openssl rand -hex 32로 생성한 값을 저장
pnpm build:shared
pnpm dev
```

Windows PowerShell에서는 `Copy-Item .env.example .env`로 복사한다. 개발 서버가 출력한 웹 주소를 열고 브라우저의 모바일 에뮬레이션에서 화면을 확인한다. 실제 기기의 마이크, 화면 공유, 백그라운드 동작은 테스트 서버와 테스트 계정으로 확인한다. 별도 모바일 클라이언트가 서버를 선택하게 할 경우 포트까지 포함한 HTTPS 주소를 입력값으로 받는다.

## 라즈베리파이 개발용 배포

Docker Compose, 별도 Tailscale 노드, Pi 호스트에서 실행 중인 Playit 에이전트가 필요하다. 템플릿은 새 테스트 설치용이다. Docker 서브넷 `10.253.245.0/24`와 로컬 UDP 포트 `3479`가 비어 있는 호스트에서 실행한다.

1. 이 디렉터리에서 `cp .env.example .env`를 실행한다. `JWT_SECRET`, `LIVEKIT_API_SECRET`, `PLAYIT_TURN_SECRET`은 각각 `openssl rand -hex 32`를 실행해 만든 값을 쓰고, `LIVEKIT_API_KEY`도 별도 랜덤 값으로 설정한다.
2. `docker compose up -d tailscale`을 실행하고 `docker compose exec tailscale tailscale up --accept-dns=false`의 로그인 링크에서 새 노드를 승인한다. 승인한 노드의 `*.ts.net` 이름을 `BACKSPACE_HOSTNAME`에 넣는다.
3. Playit에서 Custom UDP 터널 한 개를 추가하고 로컬 주소를 `127.0.0.1:3479`로 설정한다. Playit이 배정한 공인 IP와 포트를 `PLAYIT_PUBLIC_IP`, `PLAYIT_PUBLIC_PORT`에 넣는다. 로컬 포트를 바꿨다면 `PLAYIT_LOCAL_PORT`에도 반영한다.
4. 아래 명령으로 외부 공개 전에 로컬에서 관리자 계정을 만든다. 비밀번호는 입력 시 화면에 표시되지 않는다. 이 단계는 빈 데이터 볼륨에 한 번만 실행한다.

```bash
REGISTRATION_OPEN=true docker compose up -d --wait backspace
read -rp '관리자 이름: ' admin_name
read -rsp '관리자 비밀번호: ' admin_password
echo
printf '%s\n%s\n' "$admin_name" "$admin_password" |
  docker compose exec -T backspace node -e '
    const [username, password] = require("node:fs").readFileSync(0, "utf8").trimEnd().split("\n");
    fetch("http://127.0.0.1:3000/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, displayName: username }),
    }).then(response => {
      if (!response.ok) throw new Error("가입 실패 HTTP " + response.status);
      console.log("관리자 계정 생성됨");
    }).catch(error => { console.error(error.message); process.exitCode = 1; });
  '
unset admin_name admin_password
```

5. `.env`의 `REGISTRATION_OPEN=false`를 확인하고 전체 서비스를 시작한다. 이후 이 Compose 프로젝트의 Tailscale 노드에서만 Funnel을 설정한다.

```sh
docker compose config --quiet
docker compose up -d --wait
docker compose exec tailscale tailscale funnel --bg --tls-terminated-tcp=443 tcp://127.0.0.1:5349
docker compose exec tailscale tailscale funnel --bg --https=8443 http://127.0.0.1:18080
docker compose exec tailscale tailscale funnel status
```

Funnel 활성화 링크가 나오면 승인한 뒤 해당 명령을 다시 실행한다. `https://<BACKSPACE_HOSTNAME>:8443/api/health`에서 상태를 확인한다. 친구에게는 관리자 메뉴 `Administration → Instance → Registration → Invite Links`에서 가입 링크를 만든 뒤 비공개 공간 초대 링크를 함께 보낸다.

## 연결 경로

| 기능 | 경로 |
| --- | --- |
| 웹, 채팅, LiveKit 시그널링 | Funnel HTTPS 8443 → Caddy → Backspace 또는 LiveKit |
| 음성, 영상 및 화면 공유 UDP | Playit → Pi의 3479/UDP → Coturn → LiveKit |
| UDP 연결이 어려울 때 | Funnel TLS 443 → LiveKit 내장 TURN |

운영에 쓰는 `.env`, 계정과 데이터 볼륨, Tailscale 상태, Playit 인증은 각 호스트에서 관리한다. 친구가 모바일 앱을 테스트할 때는 별도 가입 계정을 사용한다.
