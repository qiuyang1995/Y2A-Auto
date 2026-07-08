#!/usr/bin/env python
# -*- coding: utf-8 -*-

import enum
import os
import tempfile
from urllib.parse import parse_qs, urlparse

import qrcode

from .utils.utils import get_api, raise_for_statement
from .utils.network import Api, Credential
from .utils.picture import Picture

API = get_api("login")


class QrCodeLoginEvents(enum.Enum):
    SCAN = "scan"
    CONF = "confirm"
    TIMEOUT = "timeout"
    DONE = "done"


class QrCodeLogin:
    def __init__(self) -> None:
        self.__qr_link = ""
        self.__qr_picture = None
        self.__qr_key = ""
        self.__credential = None

    def has_qrcode(self) -> bool:
        return self.__qr_link != ""

    def has_done(self) -> bool:
        return bool(self.__credential)

    def get_credential(self) -> Credential:
        raise_for_statement(self.has_done())
        return self.__credential

    def get_qrcode_picture(self) -> Picture:
        return self.__qr_picture

    async def generate_qrcode(self) -> None:
        api = API["qrcode"]["web"]["get_qrcode_and_token"]
        data = await Api(credential=Credential(), **api).result
        self.__qr_link = data["url"]
        self.__qr_key = data["qrcode_key"]

        qr = qrcode.QRCode()
        qr.add_data(self.__qr_link)
        img = qr.make_image()
        img_path = os.path.join(tempfile.gettempdir(), "y2a_bilibili_qrcode.png")
        img.save(img_path)
        self.__qr_picture = Picture.from_file(img_path)

    async def check_state(self) -> QrCodeLoginEvents:
        api = API["qrcode"]["web"]["get_events"]
        params = {"qrcode_key": self.__qr_key}
        events = await Api(credential=Credential(), **api).update_params(**params).result
        code = events["code"]
        if code == 86101:
            return QrCodeLoginEvents.SCAN
        if code == 86090:
            return QrCodeLoginEvents.CONF
        if code == 86038:
            return QrCodeLoginEvents.TIMEOUT

        query = parse_qs(urlparse(events["url"]).query)
        self.__credential = Credential(
            sessdata=(query.get("SESSDATA") or [""])[0],
            bili_jct=(query.get("bili_jct") or [""])[0],
            dedeuserid=(query.get("DedeUserID") or [""])[0],
            ac_time_value=events.get("refresh_token"),
        )
        return QrCodeLoginEvents.DONE
