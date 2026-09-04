import os
import sys
import unittest
import tempfile
import json
from pathlib import Path

# Add scripts directory to path
skill_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(skill_dir / "scripts"))

from openalex import (
    format_author_for_citation,
    map_work_type_ris,
    map_work_type_enw,
    clean_num_str,
    write_ris,
    write_enw,
    write_html_report,
    flatten,
)


class TestExportFunctions(unittest.TestCase):

    def test_format_author_for_citation(self):
        # Regular person names
        self.assertEqual(format_author_for_citation("Alberta B. Ross"), "Ross, Alberta B.")
        self.assertEqual(format_author_for_citation("E. Hayon"), "Hayon, E.")
        self.assertEqual(format_author_for_citation("Toshikazu Ibata"), "Ibata, Toshikazu")
        self.assertEqual(format_author_for_citation("Farhataziz"), "Farhataziz")
        
        # Names with suffixes
        self.assertEqual(format_author_for_citation("Charles U. Pittman Jr."), "Pittman Jr., Charles U.")
        
        # Institutional / corporate authors (must have trailing comma for EndNote)
        inst_res = format_author_for_citation("Ind. (USA). Radiation Lab. Notre Dame Univ.")
        self.assertTrue(inst_res.endswith(","))
        self.assertIn("Radiation Lab", inst_res)

        inst_dept = format_author_for_citation("Department of Chemistry, University of Tokyo")
        self.assertTrue(inst_dept.endswith(","))

    def test_type_mappings(self):
        self.assertEqual(map_work_type_ris("journal-article"), "JOUR")
        self.assertEqual(map_work_type_ris("article"), "JOUR")
        self.assertEqual(map_work_type_ris("report"), "RPRT")
        self.assertEqual(map_work_type_ris("book-chapter"), "CHAP")
        self.assertEqual(map_work_type_ris("dissertation"), "THES")

        self.assertEqual(map_work_type_enw("journal-article"), "Journal Article")
        self.assertEqual(map_work_type_enw("report"), "Report")
        self.assertEqual(map_work_type_enw("book-chapter"), "Book Section")
        self.assertEqual(map_work_type_enw("dissertation"), "Thesis")

    def test_clean_num_str(self):
        self.assertEqual(clean_num_str(119.0), "119")
        self.assertEqual(clean_num_str("46.0"), "46")
        self.assertEqual(clean_num_str(2021), "2021")
        self.assertEqual(clean_num_str("nan"), "")
        self.assertEqual(clean_num_str(None), "")

    def test_ris_and_enw_writing(self):
        sample_rows = [
            {
                "rank": 1,
                "id": "W12345",
                "doi": "10.1021/ja00716a011",
                "title": "Sites of Attack of Hydroxyl Radicals on Amides",
                "first_author": "E. Hayon",
                "all_authors": "E. Hayon; Toshikazu Ibata; Norman N. Lichtin; Michael G. Simic",
                "n_authors": 4,
                "year": "1970",
                "type": "journal-article",
                "journal": "Journal of the American Chemical Society",
                "publisher": "American Chemical Society",
                "volume": "92",
                "issue": "13",
                "pages": "3898-3903",
                "cited_by_count": 85,
                "fwci": 2.45,
                "is_oa": True,
                "oa_status": "gold",
                "pdf_url": "https://example.com/paper.pdf",
                "landing_page_url": "https://doi.org/10.1021/ja00716a011",
                "doi_url": "https://doi.org/10.1021/ja00716a011",
                "topic": "Physical Chemistry",
                "subfield": "Radiation Chemistry",
                "field": "Chemistry",
                "keywords": "Pulse Radiolysis; Hydroxyl Radical; Acetamide",
                "abstract": "The reaction of hydroxyl radicals with simple aliphatic amides was investigated by pulse radiolysis.",
                "is_retracted": False,
                "pdf_file": "",
            },
            {
                "rank": 2,
                "id": "W67890",
                "doi": "10.2172/4445489",
                "title": "Selected specific rates of reactions of transients from water. 1. Hydrated electron",
                "first_author": "M. Anbar",
                "all_authors": "M. Anbar; Ind. (USA). Radiation Lab. Notre Dame Univ.; Alberta B. Ross",
                "n_authors": 3,
                "year": "1973",
                "type": "report",
                "journal": "",
                "publisher": "U.S. Dept. of Energy / NSRDS-NBS",
                "volume": "",
                "issue": "",
                "pages": "",
                "cited_by_count": 355,
                "fwci": 1.8,
                "is_oa": True,
                "oa_status": "gold",
                "pdf_url": "https://example.com/report.pdf",
                "landing_page_url": "https://doi.org/10.2172/4445489",
                "doi_url": "https://doi.org/10.2172/4445489",
                "topic": "Chemical Physics",
                "subfield": "Radiation Chemistry",
                "field": "Physics",
                "keywords": "Hydrated Electron; Kinetics",
                "abstract": "Tabulation of reaction rates.",
                "is_retracted": False,
                "pdf_file": "",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ris_file = tmp_path / "references.ris"
            enw_file = tmp_path / "references.enw"
            html_file = tmp_path / "report.html"

            # 1. Test RIS
            write_ris(sample_rows, ris_file)
            self.assertTrue(ris_file.exists())
            ris_text = ris_file.read_text(encoding="utf-8-sig")
            self.assertIn("TY  - JOUR", ris_text)
            self.assertIn("TY  - RPRT", ris_text)
            self.assertIn("TI  - Sites of Attack of Hydroxyl Radicals on Amides", ris_text)
            self.assertIn("AU  - Hayon, E.", ris_text)
            self.assertIn("AU  - Ibata, Toshikazu", ris_text)
            self.assertIn("SP  - 3898", ris_text)
            self.assertIn("EP  - 3903", ris_text)
            self.assertIn("DO  - 10.1021/ja00716a011", ris_text)
            self.assertIn("ER  - ", ris_text)

            # 2. Test ENW
            write_enw(sample_rows, enw_file)
            self.assertTrue(enw_file.exists())
            enw_text = enw_file.read_text(encoding="utf-8-sig")
            self.assertIn("%0 Journal Article", enw_text)
            self.assertIn("%0 Report", enw_text)
            self.assertIn("%T Sites of Attack of Hydroxyl Radicals on Amides", enw_text)
            self.assertIn("%A Hayon, E.", enw_text)
            self.assertIn("%V 92", enw_text)
            self.assertIn("%N 13", enw_text)
            self.assertIn("%P 3898-3903", enw_text)
            self.assertIn("%R 10.1021/ja00716a011", enw_text)

            # 3. Test HTML
            write_html_report(sample_rows, html_file, title="Test Report", provenance={"Query": "amides pulse radiolysis"})
            self.assertTrue(html_file.exists())
            html_text = html_file.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", html_text)
            self.assertIn("Test Report", html_text)
            self.assertIn("Sites of Attack of Hydroxyl Radicals on Amides", html_text)
            self.assertIn("searchInput", html_text)
            self.assertIn("applyFiltersAndSort", html_text)

    def test_cmd_report_offline(self):
        import subprocess
        cli = skill_dir / "scripts" / "openalex.py"

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            raw_dir = tmp_path / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)

            # Write mock results.jsonl
            mock_work = {
                "id": "https://openalex.org/W999",
                "doi": "https://doi.org/10.1016/0009-2614(82)83227-2",
                "title": "Radical cations of some amides",
                "publication_year": 1982,
                "type": "journal-article",
                "cited_by_count": 42,
                "authorships": [
                    {"author": {"display_name": "K. V. S. Rao"}},
                    {"author": {"display_name": "M. C. R. Symons"}}
                ],
                "primary_location": {
                    "source": {"display_name": "Chemical Physics Letters"}
                }
            }
            (raw_dir / "results.jsonl").write_text(json.dumps(mock_work) + "\n", encoding="utf-8")

            # Run openalex.py report <dir> --all
            cmd = [sys.executable, str(cli), "report", str(tmp_path), "--all"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"CLI error: {res.stderr}")

            # Check that deliverables exist in root
            self.assertTrue((tmp_path / "report.md").exists())
            self.assertTrue((tmp_path / "report.html").exists())
            self.assertTrue((tmp_path / "references.ris").exists())
            self.assertTrue((tmp_path / "references.enw").exists())

            ris_content = (tmp_path / "references.ris").read_text(encoding="utf-8-sig")
            self.assertIn("Radical cations of some amides", ris_content)
            self.assertIn("Rao, K. V. S.", ris_content)

            enw_content = (tmp_path / "references.enw").read_text(encoding="utf-8-sig")
            self.assertIn("Radical cations of some amides", enw_content)
            self.assertIn("Symons, M. C. R.", enw_content)


if __name__ == "__main__":
    unittest.main()
