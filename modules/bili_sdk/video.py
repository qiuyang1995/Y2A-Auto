#!/usr/bin/env python
# -*- coding: utf-8 -*-

class Video:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def get_cid(self, page_index: int = 0):
        raise NotImplementedError("Video.get_cid is not included in Y2A's internal Bilibili SDK subset")
