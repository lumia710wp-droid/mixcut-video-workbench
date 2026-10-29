from mixcut.feishu import cell_equal


def test_real_tts_number_readback_precision():
    assert cell_equal(28.0194791666666, 28.019479166666667)
    assert not cell_equal(28.01948, 28.019479166666667)
    assert not cell_equal(2.00000000000001, 2)
    assert not cell_equal(True, 1.0)
    assert not cell_equal(float("nan"), 1.0)
