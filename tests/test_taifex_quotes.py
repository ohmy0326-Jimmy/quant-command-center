from datetime import date, datetime
from unittest import TestCase
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import taifex_quotes


class TestTaifexQuotes(TestCase):

    def setUp(self):
        taifex_quotes.clear_cache()
        self.now = datetime(2026, 11, 2, 19, 45, tzinfo=ZoneInfo("Asia/Taipei"))

    def tearDown(self):
        taifex_quotes.clear_cache()

    def test_parse_txo_symbol(self):
        self.assertIsNone(taifex_quotes.parse_txo_symbol("MUU261030C00045000"))
        self.assertIsNone(taifex_quotes.parse_txo_symbol("SNXX261030C00020000"))
        self.assertIsNone(taifex_quotes.parse_txo_symbol("AAPL"))

        res1 = taifex_quotes.parse_txo_symbol("TXO202611W1C45000")
        self.assertEqual(res1, {"expiry": "202611W1", "call_put": "C", "strike": 45000.0})

        res2 = taifex_quotes.parse_txo_symbol("TXO 202611W1 C 45000")
        self.assertEqual(res2, {"expiry": "202611W1", "call_put": "C", "strike": 45000.0})

        res3 = taifex_quotes.parse_txo_symbol("TXO-202612-P-22000")
        self.assertEqual(res3, {"expiry": "202612", "call_put": "P", "strike": 22000.0})

        res4 = taifex_quotes.parse_txo_symbol("台指 202611 買權 45000")
        self.assertEqual(res4, {"expiry": "202611", "call_put": "C", "strike": 45000.0})

    def test_night_quote_newer_than_day(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            market_type = json.get("MarketType")
            if market_type == "0":
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "QuoteList": [
                            {
                                "SymbolID": "TMFK6-F",
                                "DispCName": "微臺指期116",
                                "CLastPrice": "45247.00",
                                "CDate": "20261102",
                                "CTime": "134459",
                            }
                        ]
                    },
                }
            else:
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "QuoteList": [
                            {
                                "SymbolID": "TMFK6-M",
                                "DispCName": "微臺指期116",
                                "CLastPrice": "45201.00",
                                "CDate": "20261102",
                                "CTime": "194324",
                            }
                        ]
                    },
                }
            return m

        taifex_quotes._http_post = mock_post
        q = taifex_quotes.get_futures_quote("TMF", now=self.now)
        self.assertEqual(q.price, 45201.0)
        self.assertEqual(q.session, "night")
        self.assertIn("夜盤", q.label)
        self.assertEqual(q.source, "TAIFEX")
        self.assertFalse(q.is_fallback)

    def test_option_expiry_date(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "RtCode": "0",
                "RtData": {
                    "Items": [
                        {"item": "202611W1", "dispName": "202611W1(2026/11/04)"},
                        {"item": "202611", "dispName": "202611(2026/11/18)"},
                    ]
                },
            }
            return m

        taifex_quotes._http_post = mock_post
        d1 = taifex_quotes.option_expiry_date("202611W1")
        self.assertEqual(d1, date(2026, 11, 4))

        taifex_quotes._http_post = lambda *args, **kwargs: None
        taifex_quotes.clear_cache()

        self.assertEqual(taifex_quotes.option_expiry_date("202611"), date(2026, 11, 18))
        self.assertEqual(taifex_quotes.option_expiry_date("202611W1"), date(2026, 11, 4))
        self.assertEqual(taifex_quotes.option_expiry_date("202611F1"), date(2026, 11, 6))
        self.assertEqual(taifex_quotes.option_expiry_date("202612W2"), date(2026, 12, 9))
        self.assertIsNone(taifex_quotes.option_expiry_date("invalid_expiry"))

    def test_day_quote_newer(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            market_type = json.get("MarketType")
            if market_type == "0":
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "QuoteList": [
                            {
                                "SymbolID": "TMFK6-F",
                                "DispCName": "微臺指期116",
                                "CLastPrice": "45247.00",
                                "CDate": "20261102",
                                "CTime": "134459",
                            }
                        ]
                    },
                }
            else:
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "QuoteList": [
                            {
                                "SymbolID": "TMFK6-M",
                                "DispCName": "微臺指期116",
                                "CLastPrice": "",
                                "CDate": "20261102",
                                "CTime": "",
                            }
                        ]
                    },
                }
            return m

        taifex_quotes._http_post = mock_post
        q = taifex_quotes.get_futures_quote("TMF", now=self.now)
        self.assertEqual(q.price, 45247.0)
        self.assertEqual(q.session, "day")

    def test_delivery_month_selection(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "RtCode": "0",
                "RtData": {
                    "QuoteList": [
                        {
                            "SymbolID": "TMFK6-F",
                            "DispCName": "微臺指期116",
                            "CLastPrice": "45247.00",
                            "CDate": "20261102",
                            "CTime": "134459",
                        },
                        {
                            "SymbolID": "TMFL6-F",
                            "DispCName": "微臺指期126",
                            "CLastPrice": "45300.00",
                            "CDate": "20261102",
                            "CTime": "134459",
                        },
                    ]
                },
            }
            return m

        taifex_quotes._http_post = mock_post
        q = taifex_quotes.get_futures_quote(
            "TMF", delivery_month="202612", now=self.now
        )
        self.assertEqual(q.symbol_id, "TMFL6-F")
        self.assertEqual(q.price, 45300.0)
        self.assertEqual(q.contract_month, "202612")

    def test_cid_mapping_and_multipliers(self):
        info_mxf = taifex_quotes.futures_cid_for("MXF")
        self.assertEqual(info_mxf, ("MXF", "1", 50.0))

        info_txf = taifex_quotes.futures_cid_for("TXF")
        self.assertEqual(info_txf, ("TXF", "1", 200.0))

        info_fmf = taifex_quotes.futures_cid_for("FMF")
        self.assertEqual(info_fmf, ("TMF", "1", 10.0))

    def test_api_failure_fallback(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 500
            return m

        taifex_quotes._http_post = mock_post

        q_fb = taifex_quotes.get_futures_quote(
            "TMF", fallback_price=44920.14, now=self.now
        )
        self.assertTrue(q_fb.is_fallback)
        self.assertIn("TAIEX", q_fb.source)
        self.assertEqual(q_fb.price, 44920.14)

        q_none = taifex_quotes.get_futures_quote("TMF", now=self.now)
        self.assertIsNone(q_none.price)
        self.assertEqual(q_none.source, "無報價")

    def test_settlement_fallback(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "RtCode": "0",
                "RtData": {
                    "QuoteList": [
                        {
                            "SymbolID": "TMFK6-F",
                            "DispCName": "微臺指期116",
                            "CLastPrice": "",
                            "CBidPrice1": "",
                            "CAskPrice1": "",
                            "SettlementPrice": "45227.00",
                            "CDate": "20261102",
                            "CTime": "134459",
                        }
                    ]
                },
            }
            return m

        taifex_quotes._http_post = mock_post
        q = taifex_quotes.get_futures_quote("TMF", now=self.now)
        self.assertEqual(q.price, 45227.0)
        self.assertEqual(q.price_type, "settlement")
        self.assertEqual(q.source, "結算價(備援)")
        self.assertTrue(q.is_fallback)

    def test_stock_futures_lookup(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            kind_id = json.get("KindID")
            if kind_id == "4":
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "Items": [
                            {
                                "SpotID": "2330",
                                "DispCName": "台積電期貨",
                                "DispEName": "CDF",
                                "CID": "CDF",
                            }
                        ]
                    },
                }
            elif kind_id == "8":
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "Items": [
                            {
                                "SpotID": "1477",
                                "DispCName": "小型聚陽期貨",
                                "DispEName": "SCF",
                                "CID": "SCF",
                            }
                        ]
                    },
                }
            return m

        taifex_quotes._http_post = mock_post

        res_cdf = taifex_quotes.futures_cid_for("2330")
        self.assertEqual(res_cdf, ("CDF", "4", 2000.0))

        res_scf = taifex_quotes.futures_cid_for("1477", small=True)
        self.assertEqual(res_scf, ("SCF", "8", 100.0))

    def test_option_quotes(self):
        def mock_post(url, json=None, headers=None, timeout=None):
            m = MagicMock()
            m.status_code = 200
            mtype = json.get("MarketType")
            if mtype == "0":
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "QuoteList": [
                            {
                                "SymbolID": "TXO48000K6-O",
                                "DispCName": "TXO 48000 116C",
                                "CLastPrice": "211.000",
                                "CDate": "20261102",
                                "CTime": "134459",
                            },
                            {
                                "SymbolID": "TXO48000W6-O",
                                "DispCName": "TXO 48000 116P",
                                "CLastPrice": "0.000",
                                "SettlementPrice": "478.000",
                                "CDate": "20261102",
                                "CTime": "134459",
                            },
                        ]
                    },
                }
            else:
                m.json.return_value = {
                    "RtCode": "0",
                    "RtData": {
                        "QuoteList": [
                            {
                                "SymbolID": "TXO48000W6-N",
                                "DispCName": "TXO 48000 116P",
                                "CLastPrice": "3090.000",
                                "CDate": "20261102",
                                "CTime": "194324",
                            }
                        ]
                    },
                }
            return m

        taifex_quotes._http_post = mock_post

        q_call = taifex_quotes.get_option_quote(
            48000, "C", expiry="202611", now=self.now
        )
        self.assertEqual(q_call.price, 211.0)

        q_put = taifex_quotes.get_option_quote(
            48000, "P", expiry="202611", now=self.now
        )
        self.assertEqual(q_put.price, 3090.0)

        q_missing = taifex_quotes.get_option_quote(
            50000, "C", expiry="202611", now=self.now
        )
        self.assertIsNone(q_missing.price)
        self.assertIn("無報價", q_missing.label)

    def test_parse_txo_symbol_and_math(self):
        parsed = taifex_quotes.parse_txo_symbol("TXO202611W2C45000")
        self.assertEqual(
            parsed, {"expiry": "202611W2", "call_put": "C", "strike": 45000.0}
        )

        parsed2 = taifex_quotes.parse_txo_symbol("TXO 202611 C 45000")
        self.assertEqual(
            parsed2, {"expiry": "202611", "call_put": "C", "strike": 45000.0}
        )

        d_call = taifex_quotes.black76_delta(23000, 23000, 0.1, 0.2, "C")
        self.assertTrue(0 < d_call < 1)

        d_put = taifex_quotes.black76_delta(23000, 23000, 0.1, 0.2, "P")
        self.assertTrue(-1 < d_put < 0)

        # Round trip: price an option at 20% vol, then recover the vol.
        import math
        F, K, T, v = 23000.0, 23500.0, 0.1, 0.2
        d1 = (math.log(F / K) + 0.5 * v * v * T) / (v * math.sqrt(T))
        d2 = d1 - v * math.sqrt(T)
        N = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
        call_px = F * N(d1) - K * N(d2)
        vol_rt = taifex_quotes.implied_vol_black76(call_px, F, K, T, "C")
        self.assertIsNotNone(vol_rt)
        self.assertAlmostEqual(vol_rt, 0.2, delta=1e-4)
        self.assertIsNone(taifex_quotes.implied_vol_black76(0.0, F, K, T, "C"))

        delta_from_q = taifex_quotes.option_delta_from_quote(
            option_price=300.0,
            futures_price=23000.0,
            strike=23000.0,
            expiry_date=date(2026, 11, 18),
            call_put="C",
            now=self.now,
        )
        self.assertIsNotNone(delta_from_q)
        self.assertTrue(0.4 < delta_from_q < 0.6)

    def test_cache_ttl_and_reset(self):
        call_count = 0

        def mock_post(url, json=None, headers=None, timeout=None):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "RtCode": "0",
                "RtData": {
                    "QuoteList": [
                        {
                            "SymbolID": "TMFK6-F",
                            "DispCName": "微臺指期116",
                            "CLastPrice": "45247.00",
                            "CDate": "20261102",
                            "CTime": "134459",
                        }
                    ]
                },
            }
            return m

        taifex_quotes._http_post = mock_post

        taifex_quotes.get_futures_quote("TMF", now=self.now)
        initial_calls = call_count
        self.assertGreater(initial_calls, 0)

        taifex_quotes.get_futures_quote("TMF", now=self.now)
        self.assertEqual(call_count, initial_calls)

        taifex_quotes.clear_cache()
        taifex_quotes.get_futures_quote("TMF", now=self.now)
        self.assertGreater(call_count, initial_calls)
