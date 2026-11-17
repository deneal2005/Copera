"""Small QR encoder for short public shop URLs (versions 1–5, error level L)."""

import html

CAPACITY = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20), 5: (108, 26)}
ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30]}


def _gf_mul(left: int, right: int) -> int:
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        right >>= 1
        left = (left << 1) ^ (0x11D if left & 0x80 else 0)
    return result


def _ecc(data: list[int], degree: int) -> list[int]:
    generator = [1]
    root = 1
    for _ in range(degree):
        product = [0] * (len(generator) + 1)
        for index, coefficient in enumerate(generator):
            product[index] ^= coefficient
            product[index + 1] ^= _gf_mul(coefficient, root)
        generator = product
        root = _gf_mul(root, 2)
    remainder = [0] * degree
    for value in data:
        factor = value ^ remainder.pop(0)
        remainder.append(0)
        for index, coefficient in enumerate(generator[1:]):
            remainder[index] ^= _gf_mul(coefficient, factor)
    return remainder


def _format(matrix: list[list[bool | None]], mask: int) -> None:
    size = len(matrix)
    data = (1 << 3) | mask  # error correction level L, followed by mask number
    remainder = data
    for _ in range(10):
        remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
    bits = ((data << 10) | remainder) ^ 0x5412

    def bit(index: int) -> bool:
        return bool((bits >> index) & 1)

    for index in range(6):
        matrix[index][8] = bit(index)
    matrix[7][8] = bit(6)
    matrix[8][8] = bit(7)
    matrix[8][7] = bit(8)
    for index in range(9, 15):
        matrix[8][14 - index] = bit(index)
    for index in range(8):
        matrix[8][size - 1 - index] = bit(index)
    for index in range(8, 15):
        matrix[size - 15 + index][8] = bit(index)
    matrix[size - 8][8] = True


def _encode(text: str) -> list[list[bool]]:
    payload = text.encode("utf-8")
    version = next((v for v, (capacity, _) in CAPACITY.items() if len(payload) <= capacity - 2), None)
    if version is None:
        raise ValueError("Shop URL is too long for the built-in QR code. Use a shorter public host or shop URL.")
    data_count, ecc_count = CAPACITY[version]
    bits: list[int] = []

    def append(value: int, length: int) -> None:
        bits.extend((value >> shift) & 1 for shift in range(length - 1, -1, -1))

    append(0b0100, 4)  # byte mode
    append(len(payload), 8)
    for byte in payload:
        append(byte, 8)
    bits.extend([0] * min(4, data_count * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    data = [sum(bits[start + offset] << (7 - offset) for offset in range(8))
            for start in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(data) < data_count:
        data.append(pad[(len(data) - ((len(bits) + 7) // 8)) % 2])
    codewords = data + _ecc(data, ecc_count)
    stream = [(byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)]

    size = 17 + 4 * version
    matrix: list[list[bool | None]] = [[None] * size for _ in range(size)]

    def set_module(x: int, y: int, value: bool) -> None:
        if 0 <= x < size and 0 <= y < size:
            matrix[y][x] = value

    for left, top in ((0, 0), (size - 7, 0), (0, size - 7)):
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                x, y = left + dx, top + dy
                if not (0 <= x < size and 0 <= y < size):
                    continue
                if 0 <= dx < 7 and 0 <= dy < 7:
                    distance = max(abs(dx - 3), abs(dy - 3))
                    set_module(x, y, distance == 3 or distance <= 1)
                else:
                    set_module(x, y, False)
    for position in range(8, size - 8):
        set_module(6, position, position % 2 == 0)
        set_module(position, 6, position % 2 == 0)
    for center_y in ALIGNMENT[version]:
        for center_x in ALIGNMENT[version]:
            if matrix[center_y][center_x] is not None:
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_module(center_x + dx, center_y + dy, max(abs(dx), abs(dy)) != 1)
    _format(matrix, 0)  # reserves format-information cells before data placement

    bit_index = 0
    right = size - 1
    upward = True
    while right >= 1:
        if right == 6:
            right = 5
        for vertical in range(size):
            y = size - 1 - vertical if upward else vertical
            for offset in (0, 1):
                x = right - offset
                if matrix[y][x] is None:
                    value = bool(stream[bit_index]) if bit_index < len(stream) else False
                    if (x + y) % 2 == 0:  # QR mask pattern 0
                        value = not value
                    matrix[y][x] = value
                    bit_index += 1
        upward = not upward
        right -= 2
    _format(matrix, 0)
    return [[bool(cell) for cell in row] for row in matrix]


def svg(text: str, foreground: str = "#183b35", background: str = "#ffffff", scale: int = 8) -> str:
    matrix = _encode(text)
    border = 4
    size = len(matrix) + border * 2
    color = html.escape(foreground, quote=True)
    backdrop = html.escape(background, quote=True)
    modules = []
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                modules.append(f'<rect x="{x + border}" y="{y + border}" width="1" height="1"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size * scale}" height="{size * scale}" '
            f'viewBox="0 0 {size} {size}" role="img" aria-label="Shop QR code" shape-rendering="crispEdges">'
            f'<rect width="{size}" height="{size}" fill="{backdrop}"/>'
            f'<g fill="{color}">{"".join(modules)}</g></svg>')
