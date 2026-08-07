// AI source reconstruction by app.dedaub.com
// 2026.03.12 01:55 UTC

pragma solidity 0.8.19;

interface IERC20 {
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 value) external returns (bool);
    function transfer(address to, uint256 value) external returns (bool);
}

interface IWETH is IERC20 {
    function withdraw(uint256) external;
}

interface IAaveV3Pool {
    function flashLoan(
        address receiverAddress,
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata interestRateModes,
        address onBehalfOf,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(address collateralAsset, address debtAsset, address user, uint256 debtToCover, bool receiveAToken) external;
}

interface IAaveV2Pool {
    function liquidationCall(address collateralAsset, address debtAsset, address user, uint256 debtToCover, bool receiveAToken) external;
}

interface IBalancerVault {
    function flashLoan(address recipient, address[] memory tokens, uint256[] memory amounts, bytes memory userData) external;
}

interface IComptrollerLike {
    function absorb(address absorber, address[] calldata accounts) external;
    function buyCollateral(address asset, uint256 minAmount, uint256 baseAmount, address recipient) external;
}

interface IMorphoLike {
    function flashLoan(address receiver, address token, uint256 amount, bytes calldata data) external;
    function idToMarketParams(bytes32 id) external view returns (address, address, address, address, uint256);
    function liquidate(address, address, address, address, uint256, address, uint256, uint256, bytes calldata) external returns (uint256, uint256);
}

interface ICompoundV2CToken {
    function liquidateBorrow(address borrower, uint256 repayAmount, address cTokenCollateral) external returns (uint256);
}

interface ISiloHook {
    function repayFor(address asset, address user, uint256 shareAmount) external;
}

contract DecompiledLiquidator {
    mapping(address => bool) private __approvedOperators;
    address private _wETH;
    address private stor_2_0_19;
    address private _owner;

    address private constant AGGREGATOR = 0xbBbBBBB520d69a9775E85b458C58C648259FAD5F;
    address private constant AAVE_V3_POOL = 0x87870Bca3F3fD6335C3f4cE8392D69350B4fA4E2;
    address private constant AAVE_V2_POOL = 0x7d2768dE32b0B80B7a3454c06BdAc94A69DdC7A9;
    address private constant MORPHO = 0xbBbBBBBbb9cc5e90e3b3af64bdaf62C37EEFFCb;
    address private constant BALANCER_VAULT = 0xBA12222222228d8Ba445958a75a0704d566BF2C8;
    address private constant RADIANT = 0xc13e21B648A5Ee794902342038FF3aDAB66BE987;
    address private constant SILO = 0x96eFDf95cc47Fe90e8F63D2F5Ef9Fb8B180Daeb9;

    event Received(uint256 value);
    event Profit(uint256 value);

    struct LenderCfg {
        uint8 exchange;
        uint8 flashType;
        address[] debtMarkets;
        address[] collateralMarkets;
        address borrower;
        uint256[] repayAmounts;
        address[] seizeMarkets;
        uint256[] auxAmounts;
        uint256[] auxAmounts2;
    }

    struct SwapAction {
        uint256 kind;
        address tokenIn;
        address[] tokens;
        uint256[] amounts;
        address[][] pathA;
        address[][] pathB;
        uint256[][] numsA;
        uint256[][] numsB;
        address receiver;
        bytes extraData;
        uint256 minOut;
    }

    struct ExternalCall {
        bytes data;
        uint256 value;
    }

    struct ExecCfg {
        SwapAction action;
        ExternalCall[] calls;
        uint256 coinbaseAmount;
    }

    constructor(address weth_, address receiver_) {
        _owner = msg.sender;
        _wETH = weth_;
        stor_2_0_19 = receiver_;
    }

    function owner() public view returns (address) {
        return _owner;
    }

    function WETH() public view returns (address) {
        return _wETH;
    }

    function x485eba9b() public view returns (address) {
        return stor_2_0_19;
    }

    function _approvedOperators(address a) public view returns (bool) {
        return __approvedOperators[a];
    }

    function changeOwner(address newOwner) public {
        require(msg.sender == _owner, "not owner");
        _owner = newOwner;
    }

    function x2cac7d29(address[] calldata ops) public {
        require(msg.sender == _owner, "not owner");
        for (uint256 i; i < ops.length; i++) {
            __approvedOperators[ops[i]] = true;
        }
    }

    function xff7305f6(address[] calldata ops) public {
        require(msg.sender == _owner, "not owner");
        for (uint256 i; i < ops.length; i++) {
            __approvedOperators[ops[i]] = false;
        }
    }

    function x597ba779(address[] calldata tokens) public {
        require(msg.sender == _owner, "not owner");
        for (uint256 i; i < tokens.length; i++) {
            _forceApprove(tokens[i], AGGREGATOR, type(uint256).max);
            _forceApprove(tokens[i], AAVE_V3_POOL, type(uint256).max);
            _forceApprove(tokens[i], MORPHO, type(uint256).max);
            _forceApprove(tokens[i], AAVE_V2_POOL, type(uint256).max);
            _forceApprove(tokens[i], RADIANT, type(uint256).max);
        }
    }

    function xf4b4b6f8(address[] calldata tokens) public {
        require(msg.sender == _owner, "not owner");
        for (uint256 i; i < tokens.length; i++) {
            _forceApprove(tokens[i], AGGREGATOR, 0);
            _forceApprove(tokens[i], AAVE_V3_POOL, 0);
            _forceApprove(tokens[i], MORPHO, 0);
            _forceApprove(tokens[i], AAVE_V2_POOL, 0);
        }
    }

    function x753334be(address[] calldata spenders, address[] calldata tokens) public {
        require(msg.sender == _owner, "not owner");
        for (uint256 i; i < spenders.length; i++) {
            for (uint256 j; j < tokens.length; j++) {
                _forceApprove(tokens[j], spenders[i], 0);
            }
        }
    }

    function xab5430ff(address[] calldata spenders, address[] calldata tokens) public {
        require(msg.sender == _owner, "not owner");
        for (uint256 i; i < spenders.length; i++) {
            for (uint256 j; j < tokens.length; j++) {
                _forceApprove(tokens[j], spenders[i], type(uint256).max);
            }
        }
    }

    function x8ec7a33a(address[] calldata tokens, uint256[] calldata amounts) public {
        require(tokens.length == amounts.length);
        for (uint256 i; i < tokens.length; i++) {
            _safeCallOptionalReturn(tokens[i], abi.encodeWithSelector(IERC20.transfer.selector, stor_2_0_19, amounts[i]));
        }
    }

    function xe2ec3233(uint256 amount) public {
        stor_2_0_19.call{value: amount}("");
    }

    function xd8f7ffd4(LenderCfg calldata lcfg, ExecCfg calldata ecfg, uint256 coinbaseTip) public {
        require(__approvedOperators[tx.origin], "NotOp");
        require(lcfg.debtMarkets.length == lcfg.collateralMarkets.length, "invalid length");
        require(lcfg.collateralMarkets.length == lcfg.repayAmounts.length, "invalid length");
        require(lcfg.seizeMarkets.length == lcfg.auxAmounts.length, "invalid length for flash loans");

        bytes memory packed = abi.encode(ecfg, lcfg);
        if (lcfg.flashType == 0) {
            uint256[] memory modes = new uint256[](lcfg.seizeMarkets.length);
            IAaveV3Pool(AAVE_V3_POOL).flashLoan(address(this), lcfg.seizeMarkets, lcfg.auxAmounts, modes, address(this), packed, 0);
        } else if (lcfg.flashType == 1) {
            IBalancerVault(BALANCER_VAULT).flashLoan(address(this), lcfg.seizeMarkets, lcfg.auxAmounts, packed);
        } else if (lcfg.flashType == 2) {
            require(lcfg.seizeMarkets.length > 0 && lcfg.auxAmounts.length > 0);
            IMorphoLike(MORPHO).flashLoan(address(this), lcfg.seizeMarkets[0], lcfg.auxAmounts[0], packed);
        }

        require(ecfg.action.numsB.length > 0 && ecfg.action.numsB[0].length > 0);
        uint256 n = ecfg.action.numsB[0].length - 1;
        IWETH(_wETH).withdraw(coinbaseTip);
        _sendNativeToCoinbase(coinbaseTip);
        uint256 payout = ecfg.action.numsB[0][n] - coinbaseTip;
        IERC20(_wETH).transfer(stor_2_0_19, payout);
        emit Profit(payout);
    }

    function executeOperation(address[] calldata, uint256[] calldata, uint256[] calldata, address, bytes calldata params) public returns (bool) {
        require(__approvedOperators[tx.origin], "NotOp");
        (ExecCfg memory ecfg, LenderCfg memory lcfg) = _decodeAny(params);
        _executeLiquidations(ecfg, lcfg);
        _executeAggregatorSwap(ecfg);
        return true;
    }

    function receiveFlashLoan(address[] calldata tokens, uint256[] calldata amounts, uint256[] calldata feeAmounts, bytes calldata userData) public {
        require(__approvedOperators[tx.origin], "NotOp");
        require(msg.sender == BALANCER_VAULT, "unauthorized sender");
        (ExecCfg memory ecfg, LenderCfg memory lcfg) = _decodeAny(userData);
        _executeLiquidations(ecfg, lcfg);
        _executeAggregatorSwap(ecfg);
        for (uint256 i; i < tokens.length; i++) {
            uint256 total = amounts[i] + feeAmounts[i];
            _safeCallOptionalReturn(tokens[i], abi.encodeWithSelector(IERC20.transfer.selector, BALANCER_VAULT, total));
        }
    }

    function onMorphoFlashLoan(uint256, bytes calldata data) public {
        _onMorpho(data);
    }

    function siloLiquidationCallback(address user, address[] calldata assets, uint256[] calldata, uint256[] calldata shareAmountsToRepaid, bytes calldata flashReceiverData) public {
        require(__approvedOperators[tx.origin], "NotOp");
        require(msg.sender == SILO, "not approved");
        (ExecCfg memory ecfg, ) = _decodeAny(flashReceiverData);
        _executeAggregatorSwap(ecfg);
        for (uint256 i; i < assets.length; i++) {
            if (shareAmountsToRepaid[i] > 0) {
                IERC20(assets[i]).approve(SILO, shareAmountsToRepaid[i]);
                ISiloHook(SILO).repayFor(assets[i], user, shareAmountsToRepaid[i]);
            }
        }
    }

    function _onMorpho(bytes calldata data) private {
        require(__approvedOperators[tx.origin], "NotOp");
        (ExecCfg memory ecfg, LenderCfg memory lcfg) = _decodeAny(data);
        _executeLiquidations(ecfg, lcfg);
        _executeAggregatorSwap(ecfg);
    }

    function _executeLiquidations(ExecCfg memory, LenderCfg memory lcfg) private {
        if (lcfg.exchange == 0) {
            for (uint256 i; i < lcfg.debtMarkets.length; i++) {
                IAaveV3Pool(AAVE_V3_POOL).liquidationCall(lcfg.debtMarkets[i], lcfg.collateralMarkets[i], lcfg.borrower, lcfg.repayAmounts[i], false);
            }
        } else if (lcfg.exchange == 1) {
            for (uint256 i; i < lcfg.debtMarkets.length; i++) {
                IAaveV2Pool(AAVE_V2_POOL).liquidationCall(lcfg.debtMarkets[i], lcfg.collateralMarkets[i], lcfg.borrower, lcfg.repayAmounts[i], false);
            }
        } else if (lcfg.exchange == 2) {
            for (uint256 i; i < lcfg.debtMarkets.length; i++) {
                (address a, address b, address c, address d, uint256 e) = IMorphoLike(MORPHO).idToMarketParams(bytes32(uint256(uint160(lcfg.debtMarkets[i]))));
                IMorphoLike(MORPHO).liquidate(a, b, c, d, e, lcfg.borrower, lcfg.auxAmounts2.length > 0 ? lcfg.auxAmounts2[0] : 0, 0, "");
            }
        } else if (lcfg.exchange == 3) {
            if (lcfg.debtMarkets.length > 0) {
                address[] memory accounts = new address[](1);
                accounts[0] = lcfg.borrower;
                IComptrollerLike(lcfg.debtMarkets[0]).absorb(address(this), accounts);
                for (uint256 i; i < lcfg.collateralMarkets.length; i++) {
                    IComptrollerLike(lcfg.debtMarkets[0]).buyCollateral(lcfg.collateralMarkets[i], 0, lcfg.repayAmounts[i], address(this));
                }
            }
        } else if (lcfg.exchange == 4) {
            for (uint256 i; i < lcfg.debtMarkets.length; i++) {
                ICompoundV2CToken(lcfg.debtMarkets[i]).liquidateBorrow(lcfg.borrower, lcfg.collateralMarkets[i] == address(0) ? lcfg.repayAmounts[i] : lcfg.repayAmounts[i], lcfg.seizeMarkets[i]);
            }
        } else if (lcfg.exchange == 5) {
            for (uint256 i; i < lcfg.debtMarkets.length; i++) {
                IAaveV3Pool(RADIANT).liquidationCall(lcfg.debtMarkets[i], lcfg.collateralMarkets[i], lcfg.borrower, lcfg.repayAmounts[i], false);
            }
        } else {
            revert("NotExchange");
        }
    }

    function _executeAggregatorSwap(ExecCfg memory ecfg) private {
        for (uint256 i; i < ecfg.calls.length; i++) {
            (bool ok, bytes memory ret) = AGGREGATOR.call{value: ecfg.calls[i].value}(ecfg.calls[i].data);
            require(ok, string(ret));
        }
    }

    function _decodeLE(bytes calldata data) external pure returns (LenderCfg memory, ExecCfg memory) {
        return abi.decode(data, (LenderCfg, ExecCfg));
    }

    function _decodeEL(bytes calldata data) external pure returns (ExecCfg memory, LenderCfg memory) {
        return abi.decode(data, (ExecCfg, LenderCfg));
    }

    function _decodeAny(bytes calldata data) private returns (ExecCfg memory ecfg, LenderCfg memory lcfg) {
        try this._decodeEL(data) returns (ExecCfg memory e, LenderCfg memory l) {
            return (e, l);
        } catch {
            (LenderCfg memory l2, ExecCfg memory e2) = this._decodeLE(data);
            return (e2, l2);
        }
    }

    function _forceApprove(address token, address spender, uint256 amount) private {
        if (amount != 0) {
            uint256 current = IERC20(token).allowance(address(this), spender);
            require(current == 0, "SafeERC20: approve from non-zero to non-zero allowance");
        }
        _safeCallOptionalReturn(token, abi.encodeWithSelector(IERC20.approve.selector, spender, amount));
    }

    function _safeCallOptionalReturn(address token, bytes memory data) private {
        require(address(this).balance >= 0, "Address: insufficient balance for call");
        require(token.code.length > 0, "Address: call to non-contract");
        (bool ok, bytes memory ret) = token.call(data);
        require(ok, "SafeERC20: low-level call failed");
        if (ret.length > 0) {
            require(abi.decode(ret, (bool)), "SafeERC20: ERC20 operation did not succeed");
        }
    }

    function _sendNativeToCoinbase(uint256 amount) private {
        if (amount > 0) {
            block.coinbase.call{value: amount}("");
        }
    }

    receive() external payable {
        emit Received(msg.value);
    }

    fallback() external payable {
        emit Received(msg.value);
    }
}