from mint_engine.config.chains import chain_id_from_opensea, get_chain, public_rpc_urls


def test_arc_chain_preset():
    chain = get_chain(5042)
    assert chain.key == "arc"
    assert chain.native_symbol == "USDC"
    assert chain.public_rpc == "https://rpc.mainnet.arc.io"
    assert "https://rpc.mainnet.arc.io" in public_rpc_urls(5042)


def test_opensea_chain_arc():
    assert chain_id_from_opensea("arc") == 5042
