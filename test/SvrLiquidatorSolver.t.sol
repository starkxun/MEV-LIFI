// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../contracts/SvrLiquidatorSolver.sol";

/*
 * 在 Arbitrum 历史区块上重放一笔真实的 SVR 清算。
 *
 * 复现的目标(实测数据,来自 svr_probe.py anatomy):
 *   tx      0x3125e734…  区块 468923284
 *   还      376,499.058704 USDC
 *   拿      5.566116 WBTC(collateralOut = 556611648,8 位小数)
 *   毛奖励  $26,354.93   ← 160 天里最大的一笔
 *
 * ⚠️ 关键的时序问题:
 *   真实交易里 [预言机更新 + 清算] 在**同一个区块**。
 *   所以 fork 到 468923283(前一块)时,价格还是旧的,仓位是**健康的**。
 *   测试因此分两步:
 *     1. 先证明"此刻不可清算" —— 确认 fork 点和参数都对
 *     2. 再把价格打下去,证明清算流程能跑通
 *
 * 跑:
 *   forge test --match-contract SvrLiquidatorSolverTest -vv \
 *     --fork-url https://rpc.ankr.com/arbitrum/$ANKR_KEY
 */

interface IAaveOracleLike {
    function getAssetPrice(address asset) external view returns (uint256);
}

interface IPoolLike {
    function getUserAccountData(address user)
        external view returns (uint256, uint256, uint256, uint256, uint256, uint256);
}

/// 把抵押物吃掉、吐出预设数量债务资产的假 router。
/// 用它把「Aave 接口是否正确」和「DEX 路径能不能成交」两件事分开测。
contract MockRouter {
    address public immutable tokenIn;
    address public immutable tokenOut;
    uint256 public amountOut;

    constructor(address in_, address out_) {
        tokenIn = in_;
        tokenOut = out_;
    }

    function setAmountOut(uint256 v) external { amountOut = v; }

    function swap() external {
        uint256 bal = IERC20(tokenIn).balanceOf(msg.sender);
        // 走 approve + transferFrom,和真实 router 行为一致
        (bool ok, ) = tokenIn.call(
            abi.encodeWithSignature("transferFrom(address,address,uint256)", msg.sender, address(this), bal));
        require(ok, "pull failed");
        IERC20(tokenOut).transfer(msg.sender, amountOut);
    }
}

contract SvrLiquidatorSolverTest is Test {
    // ── 全部来自链上实测,不是编的 ──────────────────────────────
    address constant POOL   = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address constant ORACLE = 0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7;
    address constant ATLAS  = 0x8ad1aE9D97C79aA68A0a151E83ff3942f68F86C1;

    address constant COLLATERAL = 0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f;  // WBTC
    address constant DEBT       = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;  // USDC
    address constant VICTIM     = 0x0F38a8FE95827CE9a13Bf075487ac8188c819AA4;

    uint256 constant DEBT_TO_COVER   = 376499058704;   // 376,499.058704 USDC
    uint256 constant COLLATERAL_OUT  = 556611648;      // 5.566116 WBTC
    uint256 constant FORK_BLOCK      = 468923283;      // 清算发生的前一块

    SvrLiquidatorSolver solver;
    MockRouter router;

    function setUp() public {
        solver = new SvrLiquidatorSolver(ATLAS, POOL);
        router = new MockRouter(COLLATERAL, DEBT);
        solver.setRouter(address(router), true);
    }

    // ══════════════════════════════════════════════════════════
    //  不需要 fork 的测试
    // ══════════════════════════════════════════════════════════

    /// 只有 Atlas 能调回调。没这道闸,任何人都能驱动本合约做任意 swap。
    function test_OnlyAtlasCanCall() public {
        vm.expectRevert(SvrLiquidatorSolver.NotAtlas.selector);
        solver.atlasSolverCall(
            address(this), address(this), address(0), 0, "", "");
    }

    /// 出价身份必须是自己 —— 押金记在 solverFrom 名下,
    /// 不校验的话别人能用他们的 solverOp 花我们的押金。
    function test_RejectsForeignSolverIdentity() public {
        vm.prank(ATLAS);
        vm.expectRevert(SvrLiquidatorSolver.WrongSolverIdentity.selector);
        solver.atlasSolverCall(
            address(0xBAD), address(this), address(0), 0, "", "");
    }

    /// 出价封顶。估值程序算错时的最后一道闸。
    function test_BidCapEnforced() public {
        // 注意:先把 maxBidWei() 读进局部变量。
        // vm.prank 只对**下一次外部调用**生效,如果在 expectRevert 的参数里
        // 调 solver.maxBidWei(),那次调用会把 prank 消耗掉,
        // 真正的 atlasSolverCall 就变成了普通调用者 -> 报 NotAtlas 而不是 BidTooHigh。
        uint256 cap = solver.maxBidWei();
        vm.prank(ATLAS);
        vm.expectRevert(abi.encodeWithSelector(
            SvrLiquidatorSolver.BidTooHigh.selector, 100 ether, cap));
        solver.atlasSolverCall(
            address(this), address(this), address(0), 100 ether, "", "");
    }

    /// 非原生币出价直接拒绝,而不是"猜一个处理方式"。
    function test_RejectsNonNativeBidToken() public {
        vm.prank(ATLAS);
        vm.expectRevert(abi.encodeWithSelector(
            SvrLiquidatorSolver.UnsupportedBidToken.selector, DEBT));
        solver.atlasSolverCall(address(this), address(this), DEBT, 1, "", "");
    }

    // ══════════════════════════════════════════════════════════
    //  fork 测试
    // ══════════════════════════════════════════════════════════

    function _fork() internal {
        string memory url = vm.envString("ARB_RPC_URL");
        vm.createSelectFork(url, FORK_BLOCK);
        // fork 之后重新部署,让合约落在 fork 的状态上
        solver = new SvrLiquidatorSolver(ATLAS, POOL);
        router = new MockRouter(COLLATERAL, DEBT);
        solver.setRouter(address(router), true);
    }

    /// 第一步:确认 fork 点正确 —— 此刻仓位应该还是**健康**的。
    /// 这一步不是形式主义:如果这里就已经可清算,说明我 fork 错了区块,
    /// 后面的测试全部无意义。
    function test_Fork_PositionHealthyBeforeOracleUpdate() public {
        _fork();
        (, , , , , uint256 hf) = IPoolLike(POOL).getUserAccountData(VICTIM);
        emit log_named_decimal_uint("HF at fork block", hf, 18);
        assertGt(hf, 1e18, "already liquidatable at fork block -> wrong block");
    }

    /// 第二步:把抵押物价格打下去,复现"预言机更新之后"的状态,
    /// 然后跑完整的闪电贷 -> 清算 -> 变现流程。
    function test_Fork_LiquidationFlowSucceeds() public {
        _fork();

        uint256 p0 = IAaveOracleLike(ORACLE).getAssetPrice(COLLATERAL);
        emit log_named_uint("collateral price before (1e8)", p0);

        // 模拟预言机把 WBTC 价格下调,使仓位跌破清算线。
        // 真实世界里这一步是 Atlas 把预言机更新交易排在我们前面完成的。
        uint256 p1 = (p0 * 88) / 100;
        vm.mockCall(ORACLE,
            abi.encodeWithSelector(IAaveOracleLike.getAssetPrice.selector, COLLATERAL),
            abi.encode(p1));

        (, , , , , uint256 hf) = IPoolLike(POOL).getUserAccountData(VICTIM);
        emit log_named_decimal_uint("HF after -12% price", hf, 18);
        assertLt(hf, 1e18, "still healthy after price drop -> bad params or mock");

        // 给 MockRouter 备好变现要吐出来的 USDC
        uint256 proceeds = DEBT_TO_COVER * 107 / 100;   // 比债务多 7%,覆盖闪电贷费用
        deal(DEBT, address(router), proceeds);
        router.setAmountOut(proceeds);

        // 给 solver 备一点原生币付中标价
        vm.deal(address(solver), 1 ether);

        // Atlas.reconcile 要求处于真实 metacall 的正确阶段,
        // 裸调会 revert WrongPhase() (0xe2586bcc) —— 这是**测试环境的边界**,
        // 不是我们合约的 bug。要真跑通它得把整个 Atlas metacall
        // (userOp + solverOp + 签名 + bundler)都搭起来,超出本测试范围。
        // 这里 mock 掉,专注验证"闪电贷 -> 清算 -> 变现 -> 付中标价"这条主线。
        vm.mockCall(ATLAS,
            abi.encodeWithSelector(IAtlas.reconcile.selector), abi.encode(uint256(0)));

        bytes memory params = abi.encode(SvrLiquidatorSolver.LiqParams({
            collateralAsset:  COLLATERAL,
            debtAsset:        DEBT,
            user:             VICTIM,
            debtToCover:      DEBT_TO_COVER,
            minCollateralOut: COLLATERAL_OUT * 90 / 100,   // 允许比真实值略少
            swapTarget:       address(router),
            swapData:         abi.encodeWithSignature("swap()"),
            minDebtBack:      DEBT_TO_COVER                  // 至少要能还上闪电贷本金
        }));

        uint256 before = IERC20(DEBT).balanceOf(address(solver));

        vm.prank(ATLAS);
        solver.atlasSolverCall(
            address(this),        // solverOpFrom == OWNER(测试合约部署的 solver)
            address(0xEE),        // 执行环境,收中标价
            address(0),           // bidToken = 原生币
            0.01 ether,           // bidAmount
            params,
            ""
        );

        emit log_named_uint("solver USDC after flow", IERC20(DEBT).balanceOf(address(solver)));
        emit log_named_uint("bid received by EE (wei)", address(0xEE).balance);
        assertEq(address(0xEE).balance, 0.01 ether, "bid was not paid");
        assertGe(IERC20(DEBT).balanceOf(address(solver)), before, "lost principal");
    }

    /// 第三步:滑点保护必须真的会拦。
    /// **这正是我们反编译的那个真实 bot 缺失的东西。**
    function test_Fork_SlippageGuardReverts() public {
        _fork();

        uint256 p0 = IAaveOracleLike(ORACLE).getAssetPrice(COLLATERAL);
        vm.mockCall(ORACLE,
            abi.encodeWithSelector(IAaveOracleLike.getAssetPrice.selector, COLLATERAL),
            abi.encode((p0 * 88) / 100));

        deal(DEBT, address(router), DEBT_TO_COVER * 2);
        router.setAmountOut(DEBT_TO_COVER * 107 / 100);
        vm.deal(address(solver), 1 ether);
        vm.mockCall(ATLAS,
            abi.encodeWithSelector(IAtlas.reconcile.selector), abi.encode(uint256(0)));

        // 把 minCollateralOut 设成真实产出的 10 倍 —— 必须拦下来
        bytes memory params = abi.encode(SvrLiquidatorSolver.LiqParams({
            collateralAsset:  COLLATERAL,
            debtAsset:        DEBT,
            user:             VICTIM,
            debtToCover:      DEBT_TO_COVER,
            minCollateralOut: COLLATERAL_OUT * 10,        // ← 不可能达到
            swapTarget:       address(router),
            swapData:         abi.encodeWithSignature("swap()"),
            minDebtBack:      DEBT_TO_COVER
        }));

        vm.prank(ATLAS);
        vm.expectRevert();     // 闪电贷回调里 revert,整笔回滚
        solver.atlasSolverCall(
            address(this), address(0xEE), address(0), 0.01 ether, params, "");

        assertEq(address(0xEE).balance, 0, "reverted but bid still paid");
    }
}
