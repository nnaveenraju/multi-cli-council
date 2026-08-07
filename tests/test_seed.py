from council.seed import Seed, build_seed, parse_seed_markdown


def test_parse_seed_markdown():
    text = """# My Topic

## Main points
- Alpha
- Beta

## Links
- https://example.com/a
- https://example.com/b

## Goals
- Be brief
"""
    seed = parse_seed_markdown(text)
    assert seed.title == "My Topic"
    assert seed.main_points == ["Alpha", "Beta"]
    assert seed.seed_links == ["https://example.com/a", "https://example.com/b"]
    assert seed.goals == ["Be brief"]


def test_build_seed_links_csv():
    seed = build_seed(
        title="T",
        points=["p1"],
        links=["https://a.com,https://b.com"],
    )
    assert seed.seed_links == ["https://a.com", "https://b.com"]


def test_seed_yaml_roundtrip(tmp_path):
    seed = Seed(title="T", main_points=["x"], seed_links=["https://z.com"])
    p = tmp_path / "seed.yaml"
    p.write_text(seed.to_yaml(), encoding="utf-8")
    loaded = build_seed(seed_file=p)
    assert loaded.title == "T"
    assert loaded.main_points == ["x"]
