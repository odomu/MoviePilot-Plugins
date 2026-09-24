import importlib.util
import os
import unittest

# 加载 MediaFileParser
file_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/utils/file_parser.py"))
spec = importlib.util.spec_from_file_location("cloudsubscribe.utils.file_parser", file_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

MediaFileParser = mod.MediaFileParser


class TestMediaFileParser(unittest.TestCase):
    """
    媒体文件解析模块测试用例
    """

    def test_is_video_and_subtitle(self):
        # 视频格式测试
        self.assertTrue(MediaFileParser.is_video("Movie.2024.1080p.mkv"))
        self.assertTrue(MediaFileParser.is_video("Episode.mp4"))
        self.assertTrue(MediaFileParser.is_video("Sample.ISO"))
        self.assertFalse(MediaFileParser.is_video("document.pdf"))
        self.assertFalse(MediaFileParser.is_video("subtitle.srt"))

        # 字幕格式测试
        self.assertTrue(MediaFileParser.is_subtitle("Sub.chs.srt"))
        self.assertTrue(MediaFileParser.is_subtitle("Sub.ass"))
        self.assertTrue(MediaFileParser.is_subtitle("Sub.vtt"))
        self.assertFalse(MediaFileParser.is_subtitle("Movie.mp4"))

    def test_custom_extensions(self):
        # 默认包含 avi 与 iso
        self.assertTrue(MediaFileParser.is_video("test.avi"))
        self.assertTrue(MediaFileParser.is_video("test.iso"))

        # 自定义扩展名配置（测试无前导点、大写、自定义格式）
        v_exts, s_exts = MediaFileParser.configure_extensions(
            video_extensions=[".mkv", "mp4", "ts", ".m2ts"],
            subtitle_extensions=["srt", ".ass", "sub"]
        )
        self.assertIn(".ts", v_exts)
        self.assertIn(".m2ts", v_exts)
        self.assertIn(".sub", s_exts)
        self.assertTrue(MediaFileParser.is_video("stream.ts"))
        self.assertTrue(MediaFileParser.is_video("STREAM.TS"))
        self.assertTrue(MediaFileParser.is_video("disc.m2ts"))
        self.assertFalse(MediaFileParser.is_video("test.avi"))

        # 恢复默认扩展名
        MediaFileParser.configure_extensions(
            video_extensions=MediaFileParser.DEFAULT_VIDEO_EXTENSIONS,
            subtitle_extensions=MediaFileParser.DEFAULT_SUBTITLE_EXTENSIONS,
        )

    def test_season_matching_and_filter(self):
        # Sxx 模式
        self.assertTrue(MediaFileParser.matches_target_season("Series.S02E01.mkv", target_season=2))
        self.assertFalse(MediaFileParser.matches_target_season("Series.S02E01.mkv", target_season=1))
        self.assertTrue(MediaFileParser.contains_other_season("Series.S02E01.mkv", target_season=1))
        self.assertFalse(MediaFileParser.contains_other_season("Series.S02E01.mkv", target_season=2))

        # 中文“第x季”模式
        self.assertTrue(
            MediaFileParser.matches_target_season("电视剧.第一季.第01集.mp4" if False else "电视剧.第 2 季.第01集.mp4",
                                                  target_season=2))
        self.assertTrue(MediaFileParser.contains_other_season("电视剧.第3季.第01集.mp4", target_season=1))

        # 英文“Season x”模式
        self.assertTrue(MediaFileParser.matches_target_season("Series.Season 4.Episode 01.mkv", target_season=4))
        self.assertTrue(MediaFileParser.contains_other_season("Series.Season 3.mkv", target_season=2))

    def test_extract_season_episode(self):
        self.assertEqual(MediaFileParser.extract_season_episode("Game.of.Thrones.S08E06.1080p.mkv"), (8, 6))
        self.assertEqual(MediaFileParser.extract_season_episode("Show.s01e12.mp4"), (1, 12))
        self.assertIsNone(MediaFileParser.extract_season_episode("Movie.2023.1080p.mkv"))

    def test_iter_files_tree_flattening(self):
        tree = [
            {
                "name": "Season 1",
                "is_dir": True,
                "children": [
                    {"name": "S01E01.mkv", "is_dir": False, "size": 1024},
                    {"name": "S01E02.mkv", "is_dir": False, "size": 2048},
                ],
            },
            {
                "name": "README.txt",
                "is_dir": False,
                "size": 100,
            }
        ]

        flattened = list(MediaFileParser.iter_files(tree))
        self.assertEqual(len(flattened), 3)

        relative_paths = [item["_relative_path"] for item in flattened]
        self.assertIn("Season 1/S01E01.mkv", relative_paths)
        self.assertIn("Season 1/S01E02.mkv", relative_paths)
        self.assertIn("README.txt", relative_paths)


if __name__ == "__main__":
    unittest.main()
