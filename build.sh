#!/usr/bin/env bash
# Render 构建脚本
set -e

# 确保使用正确的 Python 版本
python --version

# 安装依赖
pip install -r requirements.txt

echo "✅ 构建完成"
