"""Triglav terminal art: 40x62 px, 32-color palette, rendered as half-blocks."""
PALETTE = "c0c2b4999b8f877f6e6b787a6e614f5b6160505c5e4951524f483d3f484a4b3a2a3f3a3236404431383b313230283035332a222e241b252b2e25221d20272c1e23261e2020191f23211b151a1a18161b1f15181a18151211141713110f0b0e11"
ROWS = [
    "cbcccc99999c579999899999999998999cccbbbb",
    "vvvvvvvtvvtvflrnqqnqnqrqqtvvtttvvvrvvvvv",
    "tttttrrrqqnlnlllkkkkkllkknqqqqnqrtkqtttt",
    "rrtttqqqqnlkkiilikklllkkklnqqqnqrttttttv",
    "rrqtrqqnqqlifdfidkiddfkikklnqqnqqrttrrtt",
    "qrqqrqqnnnkkkkkkffkcciiiiklnnllnnqntqrtr",
    "qqqnnqnnnnkkikifiifdkfiifiklllllnqdlqrrq",
    "qqnqlnnnd9kiiki97fd339fddfkkklllnqlnqqnq",
    "qqnnnlllf5kiiiifcdfd7fdcdfiikklnnnnqnnnq",
    "qqqnnllkkifffffffdddfddcddfiikllnlnnnnqq",
    "rqnnlkkkkiffdcd969ccccccf97dfiillllnnqqn",
    "qnnnlkkkiifdccdc7799c99cdd99fffikkkllnln",
    "nnlllkiiifdddccc9977999cccddddffikkkllln",
    "nllkkiffffdd9dc9976677679999cdffiikkllln",
    "llkkiifdfdcc6399776556513677cddffidkklln",
    "kkkifffdcccc9337663343556677ccdddfikikll",
    "kkifffdcc55777565533433355669ccccdfiikik",
    "iiff97c697676363333585333335777c89dfiiif",
    "ffffc9c457653333274484752333556727cddfff",
    "fdddcc9445577655b821212b83356555279dc39f",
    "fdc79969478559dea4412248ged95775467cc69d",
    "dcc99768852114ejg8b828aagpb411274567cddd",
    "c9c97794420028johb44822ajsg72002486379cc",
    "997666e444228emog820102ahsme72144g95766c",
    "976335cga2128gooa842124ahooa844aahc36669",
    "775333da41124aoogaa214aahoha4102ao935357",
    "765533ba8224agoohaa418aghoog82124h933556",
    "665533eaa824ahsohog82bhoossga8aa4a713556",
    "555331944aagghssph02u02mjuohaaa428513333",
    "533331324aagoosje205o205epshgh8822313333",
    "3331111g5018emri617e4e518mpb77108b111333",
    "3311003ne2015ipe555000555isb3103gk101113",
    "3110005m8b215epi851000188ejc318eel300133",
    "1100005501636eui83200115empi73611d300111",
    "110001510056dpsj86311257cmum572005700111",
    "100003731235msjjj8633259emumb72125c10000",
    "000003953579mmgsub85557bjsosi75556cc1000",
    "00005dc6579lb8jopeb858deusjum98859cd5100",
    "0015fec89cjrp88pmjbb8emjujjjpe977ccf9211",
    "0015fie9ciselu88jsjebjmssb8gjpid9ceik911",
    "113diniejjeb88pppusgeuuvob843eppedklic21",
    "139iglmisjba84e858sspug8811217euiimjik72",
    "13dlggpppjggbb84215jsh412a1248bmpmsjjnd3",
    "13cpjjssphogggb8521348824ag88bghooommlf5",
    "37kqpjojjouhajgab84487bga8gogjgahpmmrrc6",
    "36irposuuvohgojjgbbb88gg8aemvugghspmqql7",
    "37inpptvupjugooohaa8gg8jsb8mjsvogstmmnf9",
    "565imrrtmisth8ljjgjoggedembhmjsusputml75",
    "559klqqmiporsgjmbegaagiiempbjjpvsjstnlc5",
    "56dfknrjppotugossgabeljillvojmpuvmmtrki6",
    "56ffktssphotvhhjjshmijojmjguumpuvtlrrnk7",
    "66illrtvjgorroggggogbahjgggpuprttttqrnk9",
    "59ilintuhgprlshhhhbghhhhoojpurrpmptqrkic",
    "7cclfktvuurrmtooojgghhghsuspustoghpmricc",
    "99fqqnpojbmtprssuuohhoohhurruuuphagjmqd9",
    "79ktvtogggptppsouuohbghooumpvvvbe8bjjqf7",
    "79ivvpphgajrpmtspuo48ojjsomruusagbggjml9",
    "77kvumphagjmropsjusbgojoopptusogaaghjjnc",
    "79ltojpjgjbmtj8sossjoohsooppsohbaaooppqk",
    "7cnrppphagagus4jsssoooogjossssggjhjosupr",
    "9flqsppjgbbhsvghvuusssugossuuugaahhosspl",
    "9cdqspopohjosvosvuvusvuuussuusojoosssuri",
]
_ALPHA = "0123456789abcdefghijklmnopqrstuv"


def _rgb(ch):
    i = _ALPHA.index(ch) * 6
    return tuple(int(PALETTE[i + k:i + k + 2], 16) for k in (0, 2, 4))


def art_lines():
    """Return list of ANSI truecolor lines (one text row = two pixel rows)."""
    out = []
    for y in range(0, len(ROWS), 2):
        top, bot = ROWS[y], ROWS[y + 1]
        line = []
        for t, b in zip(top, bot):
            (r1, g1, b1), (r2, g2, b2) = _rgb(t), _rgb(b)
            line.append(f"\x1b[38;2;{r1};{g1};{b1}m\x1b[48;2;{r2};{g2};{b2}m\u2580")
        out.append("".join(line) + "\x1b[0m")
    return out
