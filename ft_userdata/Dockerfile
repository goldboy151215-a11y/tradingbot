FROM freqtradeorg/freqtrade:stable

# Voeg Weex exchange klasse toe aan Freqtrade
USER root
COPY weex_exchange_patch.py /tmp/weex_exchange_patch.py
RUN python3 /tmp/weex_exchange_patch.py
USER ftuser
