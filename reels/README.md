# 릴스 자동 제작

사진·영상 폴더 하나로 인스타그램 릴스(1080×1920, 30fps, H.264)를 만듭니다.
스타일: 장식 없이 흰 글자 + 여백(미니멀). 글꼴은 Pretendard(OFL).

## 준비
```bash
pip install pillow numpy pyyaml pillow-heif   # pillow-heif: 아이폰 HEIC 사진용
# ffmpeg, ffprobe 필요
```

## 사용
```bash
python reels/make_reel.py 내폴더 --init --theme food   # reel.yaml 틀 생성
# reel.yaml 에 제목·자막 채우기
python reels/make_reel.py 내폴더                         # 내폴더/내폴더_reel.mp4 + 커버 jpg
```
설정 파일 없이 바로: `python reels/make_reel.py 내폴더 --theme book --title "제목"`

## 테마
| theme | 첫 화면 작은 글씨 | 마지막 문구 |
|---|---|---|
| food | 맛집 | 저장해두고 꼭 가보세요 |
| info | 알아두면 좋은 정보 (자막에 01, 02 번호) | 저장하고 필요할 때 꺼내보세요 |
| kids | 아이와 가볼 곳 | 주말 나들이 리스트에 저장 |
| book | 오늘의 책 | 읽고 싶은 책 목록에 저장 |

## 화면 구성
1. 첫 사진/영상 위에 제목(가운데)
2. 이후 사진/영상마다 아래쪽에 자막
3. 끝 화면: 이름 + 정보(위치·영업시간 등) + 마지막 문구

- 세로 사진은 화면을 꽉 채우고 천천히 확대/축소, 가로 사진·영상은 검은 배경 가운데
- 이모지는 글꼴에 없어서 자동으로 지웁니다
- 배경음악: 폴더 안 mp3 등을 자동 사용. 저작권 걱정 없이 하려면 음악 없이 만들고 인스타 앱에서 음악을 추가하세요
- 음악이 없으면 영상 원래 소리를 살립니다 (`sound: true/false` 로 클립별 지정)

## reel.yaml 예시
```yaml
theme: food
title: "창원 상남동\n숨은 파스타 맛집"
subtitle: "웨이팅해도 아깝지 않은 곳"
handle: "@my_id"
clips:
  - file: 01.jpg
  - file: 02.jpg
    text: "트러플 크림 파스타"
    sub: "18,000원"
  - file: 03.mp4
    start: 1        # 시작 지점(초)
    duration: 4     # 쓸 길이(초)
    text: "직접 뽑는 생면"
outro:
  name: "파스타공방 상남점"
  details:
    위치: "창원시 성산구 ..."
    영업시간: "11:30 – 21:00"
```
기본 길이: 사진 2.6초, 첫 화면 3.2초, 영상 최대 5초, 끝 화면 3.6초 (`duration` 으로 변경).
