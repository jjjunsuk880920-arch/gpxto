# GPXTo (구글맵 목적지를 자전거 경로 파일로)

구글맵에서 장소를 **공유**하면, 내 현재 위치에서 그 장소까지의 **도로 자전거 경로(GPX)** 를 만들고
가민 커넥트 / IGPSPORT 앱으로 넘길 수 있습니다.

## 동작 방식
1. 구글맵 → 장소 → 공유 → "GPXTo" 선택
2. 앱이 목적지 좌표를 읽고, 폰의 현재 위치를 가져옴
3. 경로 계산(BRouter, 도로 자전거 프로필 `fastbike`) → GPX 생성
4. "가민 커넥트 / IGPSPORT로 보내기" 버튼 → 폰의 공유창에서 앱 선택

## 내 컴퓨터에서 먼저 실행해 보기
```
pip install -r requirements.txt
python app.py
```
브라우저에서 http://localhost:8000 을 열고, 구글맵 링크를 붙여넣어 **경로 만들기**를 눌러 보세요.
(컴퓨터는 위치 권한 허용이 필요합니다.)

## 폰의 "공유" 메뉴에 나타나게 하기 (인터넷 주소 필요)
공유 메뉴 연동은 **https 주소**에서만 동작합니다. 무료 호스팅(예: Render.com)에 올리면 됩니다.

1. 이 폴더를 GitHub 저장소에 올림
2. Render.com → New → Web Service → 저장소 선택
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn app:app`
5. 배포가 끝나면 나오는 `https://....onrender.com` 주소를 폰 **크롬**에서 열기
6. 크롬 메뉴 → "홈 화면에 추가 / 앱 설치"
7. 이제 구글맵의 공유창에 "GPXTo"가 나타납니다

## 설정 (선택)
- `BROUTER_PROFILE` : 기본 `fastbike`(도로). `trekking`, `gravel` 등으로 변경 가능
- `BROUTER_URL` : 경로 서버 주소. 많이 쓰면 직접 서버를 운영하는 것이 예의

## 알아둘 점
- 가민 커넥트와 IGPSPORT는 개인에게 경로 전송 API를 열어 두지 않아, **공유창으로 앱에 넘기는 방식**입니다.
  앱이 GPX 파일 열기를 지원하는지는 앱 버전에 따라 다를 수 있으니, 안 되면 "GPX 파일 저장" 후
  각 앱의 코스/경로 가져오기 메뉴를 사용하세요.
- 공개 BRouter 서버는 무료이며 가끔 느리거나 응답하지 않을 수 있습니다.
- 구글맵 단축 링크에 좌표가 없는 드문 경우에는 실패할 수 있습니다. 그럴 땐 다른 장소 링크로 다시 시도하세요.
