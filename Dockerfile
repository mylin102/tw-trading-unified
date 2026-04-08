# 使用 Shioaji 官方映像檔，確保 SDK 與系統依賴 (Solace, etc.) 最佳相容
FROM sinotrade/shioaji:latest

# 切換回 root 權限以安裝專案額外依賴
USER root

# 設定時區為 Asia/Taipei
ENV TZ=Asia/Taipei
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安裝專案額外需要的系統工具
# - build-essential: Numba/llvmlite 編譯所需
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 設定工作目錄
WORKDIR /app

# 複製依賴清單並安裝
# 註：shioaji 已包含在基礎映像檔中，pip 會自動處理版本對齊
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製專案原始碼
COPY . .

# 建立必要的資料與日誌目錄
RUN mkdir -p logs/market_data exports/trades data/chips

# 預設執行交易主程式
CMD ["python", "main.py"]
