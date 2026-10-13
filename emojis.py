# ============================================================
#   emojis.py — Telegram Premium Custom Emojis Helper
#   Extracted from bot 3 luxury theme
# ============================================================

class SafeEmojiDict(dict):
    def __getitem__(self, key):
        if key in self:
            return super().__getitem__(key)
        return "5816539591812845173"  # Fallback crown emoji ID


_E = SafeEmojiDict({
    "cart":        "6021352786724133912",
    "arrow":       "5809840662601802452",
    "crown":       "5816539591812845173",
    "link":        "5767297774584339394",
    "forward":     "5803244322979585969",
    "shop":        "5312361253610475399",
    "android":     "6008224489039466126",
    "ios":         "6273556792513402505",
    "pc":          "6005916300600152073",
    "download":    "5406745015365943482",
    "money":       "5417924076503062111",
    "gear":        "5828049232287767432",
    "fire":        "5798436339019947373",
    "question":    "5314504236132747481",
    "globe":       "5956045399511797140",
    "gift":        "5785281906459283269",
    "check":       "6028565819225542441",
    "rightemo":    "6028565819225542441",
    "manemo":      "5767297774584339394",
    "infinty1":    "6298717844804733009",
    "infinity2":   "6298717844804733009",
    "adno":        "5197269100878907942",
    "lightni":     "6102879254641382280",
    "chart":       "6143271021173543049",
    "camera":      "5319175438268913255",
    "clock":       "5933544413740403607",
    "cross":       "6032606743500951856",
    "bulb":        "5472146462362048818",
    "lock":        "5393302369024882368",
    "shield":      "5393302369024882368",
    "map":         "5956045399511797140",
    "dev":         "5190498849440931467",
    "text":        "5841276284155467413",
    "wallet":      "5990147899403539264",
    "calendar":    "6014861794059752708",
    "tools":       "5449428597922079323",
    "card":        "6019224342666157570",
    "point":       "5470177992950946662",
    "warning":     "5447644880824181073",
    "vote":        "5359741159566484212",
    "idcard":      "5422683699130933153",
    "moon":        "5289930954410836372",
    "hourglass":   "5451732530048802485",
    "plus":        "6032733629719777782",
    "minus":       "5839277185022866153",
    "edit":        "5334673106202010226",
    "trash":       "5445267414562389170",
    "coin":        "5379600444098093058",
    "pen":         "5197269100878907942",
    "cash":        "6035277294036061660",
    "right":       "6129776315533893189",
    "refer":       "5271604874419647061",
    "support":     "5219693911110330533",
    "infinity":    "6298717844804733009",
    "robot":       "5190498849440931467",
    "bell":        "5809840662601802452",
    # Badges
    "prem_p":      "6269298139165889540",
    "prem_re":     "6269144224717870116",
    "prem_miu":    "6269464775307038921",
    "prem_m":      "6269520996428943568",
    "priv_p":      "6158937300932760095",
    "priv_riv":    "6158913150331654400",
    "priv_at":     "6159118192070365351",
    "priv_e":      "6156646197873350155",
    # Decorative row headers
    "deco_fly_l":  "5415938190999594673",
    "deco_snow_l1":"5415862221618059920",
    "deco_snow_l2":"5415637315655607805",
    "deco_snow_l3":"5415767770992256042",
    "deco_om_l":   "5415689185975640304",
    "deco_om_r":   "5415799536570376505",
    "deco_snow_r1":"5418027134243344774",
    "deco_snow_r2":"5415592111124813747",
    "deco_snow_r3":"5416127354244213003",
    "deco_fly_r":  "5415878933335809773",
    # Lightning / shock rows
    "shock_ncl":    "5458371041954897946",
    "shock_r":      "4972172205652706288",
    "shock_r1":     "4974451085235192775",
    "shock_r3":     "4974558532432036591",
    "shock_r4":     "4972061468510913587",
    "shock_r5":     "4972370753400865760",
    "shock_r6":     "4972135526631999147",
    "shock_ncr":    "5458371041954897946",
    # Live and Offline Status Emojis
    "livei":       "6269194596094317437",
    "offlineui":   "6269223265001017592",
    # World Emojis
    "worldd":      "5399898266265475100",
    "world2":      "6114021507908767611",
    # Dashboard & Target Custom Premium Emojis
    "pointer":     "5397782960512444700",
    "header":      "5780405967527089720",
    "mani":        "5372926953978341366",
    "messagt":     "5258500400918587241",
    "targi":       "5463274047771000031",
    "relok":       "5348536444589719796",
    "progf":       "6129801569941592173",
    "sentti":      "5445355530111437729",
    "feilfd":      "6100391433489878852",
    "trrfg":       "6141081558515190831",
    "grpoi":       "5461117441612462242",
    "chipo":       "5951766778861328139",
    "cvnrp":       "5462902520215002477",
    "blacki":      "5390928287197328330",
    "vrtit":       "6129600178925083831",
    "cutut":       "5456140674028019486",
    # Title glow grid
    "glow1":        "5332328182907445859",
    "glow2":        "5332317716072142654",
    "glow3":        "5332269736992481255",
    "glow4":        "5334580485232302082",
})

_FB = {
    "rightemo":    "✅",
    "check":       "✅",
    "manemo":      "👥",
    "infinty1":    "♾️",
    "infinity2":   "♾️",
    "infinity":    "♾️",
    "android":     "📱",
    "crown":       "👑",
    "link":        "🔗",
    "globe":       "🌐",
    "fire":        "🔥",
    "gear":        "⚙️",
    "clock":       "⏱",
    "chart":       "📊",
    "cross":       "❌",
    "arrow":       "➡️",
    "plus":        "➕",
    "minus":       "➖",
    "trash":       "🗑",
    "text":        "📝",
    "hourglass":   "⌛",
    "warning":     "⚠️",
    "dev":         "💻",
    "idcard":      "🪪",
    "adno":        "📢",
    "lightni":     "⚡",
    "livei":       "🔴",
    "offlineui":   "⚪",
    "worldd":      "🌍",
    "world2":      "🌐",
    "pointer":     "👉",
    "header":      "📢",
    "mani":        "👤",
    "messagt":     "📝",
    "targi":       "🎯",
    "relok":       "🔄",
    "progf":       "📊",
    "sentti":      "✉️",
    "feilfd":      "❌",
    "trrfg":       "🎯",
    "grpoi":       "👥",
    "chipo":       "📢",
    "cvnrp":       "🌐",
    "blacki":      "📜",
    "vrtit":       "🧩",
    "cutut":       "⚡",
}

def e(name: str, fb: str = None) -> str:
    """Render custom animated Telegram Premium emoji in HTML parse_mode."""
    emoji_id = _E.get(name)
    if not fb:
        fb = _FB.get(name, "✨")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji>'
    return fb

def get_emoji_id(name: str) -> str:
    """Get raw custom_emoji_id string for InlineKeyboardButton."""
    return _E.get(name, "")


DIGIT_EMOJIS = {
    '1': ("5409326410969940035", "1️⃣"),
    '2': ("5408935066434828852", "2️⃣"),
    '3': ("5408949493229976130", "3️⃣"),
    '4': ("5408932360605438497", "4️⃣"),
    '5': ("5408998133734606283", "5️⃣"),
    '6': ("5408827683662491596", "6️⃣"),
    '7': ("5409113337642385093", "7️⃣"),
    '8': ("5408965590767403192", "8️⃣"),
    '9': ("5409215016698149759", "9️⃣"),
    '0': ("5408876002044577145", "0️⃣"),
}


def num_to_emoji(val) -> str:
    """
    Converts numbers/digits to custom Telegram Premium emoji digits.
    Real-time dynamic conversion: e.g. 0 -> <tg-emoji>0️⃣</tg-emoji>, 123 -> <tg-emoji>1️⃣</tg-emoji>...
    """
    if val is None:
        return ""
    val_str = str(val)
    res = []
    for char in val_str:
        if char in DIGIT_EMOJIS:
            emoji_id, fb = DIGIT_EMOJIS[char]
            res.append(f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji>')
        else:
            res.append(char)
    return "".join(res)
