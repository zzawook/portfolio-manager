from rebalancer.brokers.ibkr import find_contracts
from rebalancer.envfile import load_env_file


def test_env_file_is_read_without_overriding(tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# keys\n"
        "ANTHROPIC_API_KEY=sk-ant-123\n"
        'export OPENAI_API_KEY="sk-open 456"\n'
        "IBKR_ACCOUNT_ID=DU1234567  # paper\n"
        "ALREADY=from-file\n"
        "not a line\n"
        "1BAD=x\n",
        encoding="utf-8",
    )
    env = {"ALREADY": "from-shell"}
    loaded = load_env_file(f, env)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-123"
    assert env["OPENAI_API_KEY"] == "sk-open 456"
    assert env["IBKR_ACCOUNT_ID"] == "DU1234567"
    assert env["ALREADY"] == "from-shell"
    assert set(loaded) == {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "IBKR_ACCOUNT_ID"}


def test_missing_env_file_is_fine(tmp_path):
    assert load_env_file(tmp_path / ".env", {}) == []


class SearchClient:
    def search_stock(self, symbol):
        return {
            symbol: [
                {
                    "name": "VANGUARD FTSE ALL-WORLD",
                    "contracts": [{"conid": 111, "exchange": "LSEETF"}, {"conid": 222, "exchange": "EBS"}],
                }
            ]
        }

    def contract_info(self, conid):
        return {"currency": "USD" if conid == 111 else "CHF"}


def test_contract_search_marks_the_lse_usd_line():
    found = find_contracts(SearchClient(), "vwra", "USD")
    assert [(c["conid"], c["recommended"]) for c in found] == [(111, True), (222, False)]
