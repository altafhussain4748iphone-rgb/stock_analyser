import importlib


def test_package_modules_import():
    crypto_module = importlib.import_module("technical_analysis.crypto.alerts")
    stock_module = importlib.import_module("technical_analysis.stocks.alerts")

    assert crypto_module.run_crypto_scan is not None
    assert stock_module.run_stock_scan is not None
