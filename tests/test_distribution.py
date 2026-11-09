"""Il pacchetto pubblico deve rifiutare dati clinici e credenziali anche se tracciati."""

import pytest

from scripts.check_distribution import validate_file


@pytest.mark.parametrize(
    "name",
    [
        "datasets/hosp/admissions.csv.gz",
        "src/output/bozza.txt",
        "web/.env.local",
        "scripts/access.env",
        "TEST-SPARK-QWEN/scheda.json",
        "risultati/prova/metriche.json",
        "risultati/campagna-qwen.json",
        "src/note/discharge.json",
        "web/public/archive.zip",
    ],
)
def test_private_files_are_rejected_even_if_tracked(name):
    with pytest.raises(ValueError):
        validate_file(name, b"test")


@pytest.mark.parametrize(
    "content",
    [
        "mimic-" + str(2 * 10**7 + 1),
        str(10**7 + 1) + "-DS-1",
        "APP_MIMIC_TOKEN=" + "a" * 43,
        "subject_id,hadm_id,admittime\n1,2,2100-01-01",
        "Admission Date: test\nDischarge Date: test",
        "ghp_" + "x" * 36,
    ],
)
def test_private_content_is_rejected_without_echoing_it(content):
    with pytest.raises(ValueError) as error:
        validate_file("README.md", content.encode())
    assert content not in str(error.value)


def test_clinical_payload_is_rejected_in_an_apparently_safe_summary():
    with pytest.raises(ValueError):
        validate_file("risultati/prova/riepilogo.json", b'{"results":[{"text":"privato"}]}')


def test_synthetic_fixtures_and_aggregate_counts_are_allowed():
    validate_file("tests/test_example.py", b'context = "mimic-100" # invented fixture')
    validate_file("risultati/prova/riepilogo.json", b'{"tests":30,"completed":29,"failed":1}')
    validate_file(".env.example", b"LLM_API_KEY=not-required\n")


def test_symlinks_and_modified_tokenizers_are_rejected():
    with pytest.raises(ValueError):
        validate_file("README.md", b"../private", "120000")
    with pytest.raises(ValueError):
        validate_file("src/codice_tesi/discharge/tokenizers/qwen3_8.json", b"modified")
