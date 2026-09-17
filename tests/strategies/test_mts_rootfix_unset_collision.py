def test_p0_1_uses_a_dedicated_object_sentinel_without_shadowing():
    from strategies.plugins.futures.active import tmf_spread as mod

    assert type(mod._UNSET_SENTINEL) is object
    assert mod._UNSET_SENTINEL is not mod._UNSET
    assert mod._UNSET == "UNSET"
