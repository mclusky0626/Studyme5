from app.help import TOPICS, build_help, is_help_request


def test_help_keywords():
    for t in ["도움말", "사용법?", "help", "뭐 할 수 있어?", "기능"]:
        assert is_help_request(t), t
    for t in ["도움말 좀 줄래 기말고사 공부법", "오늘 날씨 어때", "크리스마스까지 며칠?"]:
        assert not is_help_request(t), t


def test_every_topic_fits_discord_limits():
    for key in list(TOPICS) + ["start"]:
        e = build_help(key, "라스페치아")
        assert len(e.title) <= 256 and len(e.description or "") <= 4096
        assert len(e.fields) <= 25
        assert all(len(f.value) <= 1024 and len(f.name) <= 256 for f in e.fields)
        assert len(e) <= 6000
