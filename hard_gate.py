import yfinance as yf
df = yf.download(["AAPL","0700.HK","7203.T","SAP.DE"], period="5d", interval="1m")
print(df.tail())