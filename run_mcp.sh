#!/bin/bash
cd /Users/jimquant/Desktop/workspace/broker-reports
exec /Users/jimquant/miniconda3/envs/ccbot/bin/python -m src.server --transport http --host 0.0.0.0 --port 8100
