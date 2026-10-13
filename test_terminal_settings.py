"""Regression tests for edits to user terminal JSONC settings."""
import json
import unittest

import statusline as sl


class TerminalSettings(unittest.TestCase):
    def test_wt_empty_defaults_and_font_are_valid_json(self):
        for text in ('{"profiles":{"defaults":{}}}', '{"profiles":{"defaults":{"font":{}}}}'):
            with self.subTest(text=text):
                new, outcome = sl._wt_font_edit(text, 'Foo Nerd Font')
                self.assertEqual(outcome, 'set')
                self.assertEqual(json.loads(new)['profiles']['defaults']['font']['face'], 'Foo Nerd Font')

    def test_vscode_empty_object_is_valid_json(self):
        new, outcome = sl._vscode_font_edit('{}', 'Foo Nerd Font')
        self.assertEqual(outcome, 'set')
        self.assertEqual(json.loads(new), {'terminal.integrated.fontFamily': 'Foo Nerd Font'})

    def test_wt_comments_and_nested_unrelated_defaults_are_ignored(self):
        text = '''{
            // "defaults": {"font":{"face":"Comment Nerd Font"}}
            "unrelated": {"defaults":{"font":{"face":"Other Nerd Font"}}},
            "profiles": {"defaults": {"font": {
                /* "face": "Comment Nerd Font" */
                "unrelated": {"face": "Nested Nerd Font"},
                "face": "Consolas"
            }}}
        }'''
        new, outcome = sl._wt_font_edit(text, 'Foo Nerd Font')
        self.assertEqual(outcome, 'set')
        self.assertIn('"face": "Foo Nerd Font"', new)
        self.assertIn('/* "face": "Comment Nerd Font" */', new)
        self.assertIn('"face": "Nested Nerd Font"', new)
        self.assertIn('"face":"Other Nerd Font"', new)

    def test_vscode_comments_nested_keys_and_strings_are_ignored(self):
        text = '''{
            // "terminal.integrated.fontFamily": "Comment Nerd Font"
            "nested": {"terminal.integrated.fontFamily": "Nested Nerd Font"},
            "example": "\\\"terminal.integrated.fontFamily\\\": \\\"String Nerd Font\\\"",
            "terminal.integrated.fontFamily": "Consolas"
        }'''
        new, outcome = sl._vscode_font_edit(text, 'Foo Nerd Font')
        self.assertEqual(outcome, 'set')
        self.assertIn('"terminal.integrated.fontFamily": "Foo Nerd Font, Consolas"', new)
        self.assertIn('// "terminal.integrated.fontFamily": "Comment Nerd Font"', new)
        self.assertIn('"terminal.integrated.fontFamily": "Nested Nerd Font"', new)

    def test_face_names_are_json_escaped(self):
        face = 'A "Quoted" \\ Font'
        wt, _ = sl._wt_font_edit('{"profiles":{"defaults":{"font":{"face":"Consolas"}}}}', face)
        self.assertEqual(json.loads(wt)['profiles']['defaults']['font']['face'], face)
        vs, _ = sl._vscode_font_edit('{"terminal.integrated.fontFamily":"Consolas"}', face)
        self.assertEqual(json.loads(vs)['terminal.integrated.fontFamily'], face + ', Consolas')


if __name__ == '__main__':
    unittest.main()
