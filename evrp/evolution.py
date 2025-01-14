import os
import pickle
import collections

from .model import *
from .util import *
from .operation import *


class Evolution(metaclass=ABCMeta):
    def output_to_file(self, suffix: str = '') -> None:
        if not os.path.exists('result'):
            os.mkdir('result')
        if not os.path.exists('result/'+self.model.file_type):
            os.mkdir('result/'+self.model.file_type)
        filename = self.model.data_file.split('/')[-1].split('.')[0]
        output_file = open('result/{}/{}{}{}.txt'.format(self.model.file_type, filename, '' if self.model.negative_demand == 0 else '_neg'+str(self.model.negative_demand), suffix), 'a')
        output_file.write(str(self.S_best)+'\n'+str(self.S_best.sum_distance())+'\n'+str(self.S_best.feasible_detail(self.model))+'\n\n')
        output_file.close()

    def freeze_evo(self, suffix: str = '') -> None:
        if not os.path.exists('result'):
            os.mkdir('result')
        if not os.path.exists('result/'+self.model.file_type):
            os.mkdir('result/'+self.model.file_type)
        filename = self.model.data_file.split('/')[-1].split('.')[0]

        num = 1
        base_pickle_filepath = 'result/{}/{}{}_evo{}.pickle'.format(self.model.file_type, filename, '' if self.model.negative_demand == 0 else '_neg'+str(self.model.negative_demand), suffix)
        pickle_filepath = base_pickle_filepath
        while os.path.exists(pickle_filepath):
            pickle_filepath = base_pickle_filepath[:-7]+str(num)+base_pickle_filepath[-7:]
            num += 1

        pickle_file = open(pickle_filepath, 'wb')
        pickle.dump(self.freeze(), pickle_file)
        pickle_file.close()


class VNS_TS(Evolution):
    # 构造属性
    model = None

    vns_neighbour_Rts = 4
    vns_neighbour_max = 5
    eta_feas = 100
    eta_dist = 40
    Delta_SA = 0.08

    penalty = [10, 10, 10]
    penalty_min = (0.5, 0.5, 0.5)
    penalty_max = (5000, 5000, 5000)
    delta = 1.2
    eta_penalty = 2

    nu_min = 15
    nu_max = 30
    lambda_div = 1.0
    eta_tabu = 100
    # 状态属性
    vns_neighbour = []
    frequency = {}
    possible_arc = {}
    SA_dist = None
    SA_feas = None
    penalty_update_flag = []
    S_best = None

    def __init__(self, model: Model, **param) -> None:
        self.model = model
        for key, value in param.items():
            assert hasattr(self, key)
            setattr(self, key, value)

        self.SA_dist = Util.SA(self.Delta_SA, self.eta_dist)
        self.SA_feas = Util.SA(self.Delta_SA, self.eta_feas)
        self.penalty_update_flag = [collections.deque(maxlen=self.eta_penalty), collections.deque(maxlen=self.eta_penalty), collections.deque(maxlen=self.eta_penalty)]
        self.calculate_possible_arc()
        print(len(self.possible_arc))

    @staticmethod
    def penalty_capacity(route: Route, vehicle: Vehicle) -> float:
        if route.arrive_load_weight is None:
            route.cal_load_weight(vehicle)
        penalty = max(route.arrive_load_weight[0]-vehicle.capacity, 0)
        neg_demand_cus = []
        for i, cus in enumerate(route.visit):
            if cus.demand < 0:
                neg_demand_cus.append(i)
        for i in neg_demand_cus:
            penalty += max(route.arrive_load_weight[i]-vehicle.capacity, 0)
        return penalty

    @staticmethod
    def penalty_time(route: Route, vehicle: Vehicle) -> float:
        if route.arrive_time is None:
            route.cal_arrive_time(vehicle)
        late_time = route.arrive_time-np.array([cus.over_time for cus in route.visit])
        if_late = np.where(late_time > 0)[0]
        if len(if_late) > 0:
            return late_time[if_late[0]]
        else:
            return 0.0

    @staticmethod
    def penalty_battery(route: Route, vehicle: Vehicle) -> float:
        if route.arrive_remain_battery is None:
            route.cal_remain_battery(vehicle)
        return np.abs(np.sum(route.arrive_remain_battery, where=route.arrive_remain_battery < 0))

    @staticmethod
    def get_objective_route(route: Route, vehicle: Vehicle, penalty: list) -> float:
        if route.no_customer():
            return 0
        return route.sum_distance()+penalty[0]*VNS_TS.penalty_capacity(route, vehicle)+penalty[1]*VNS_TS.penalty_time(route, vehicle)+penalty[2]*VNS_TS.penalty_battery(route, vehicle)

    @staticmethod
    def get_objective(solution: Solution, model: Model, penalty: list) -> float:
        ret = 0
        for route in solution.routes:
            ret += VNS_TS.get_objective_route(route, model.vehicle, penalty)
        return ret

    def calculate_possible_arc(self) -> None:
        self.model.find_nearest_station()
        all_node_list = [self.model.depot]+self.model.rechargers+self.model.customers
        for node1 in all_node_list:
            for node2 in all_node_list:
                if (isinstance(node1, Depot) and isinstance(node2, Recharger) and (node1.x == node2.x and node1.y == node2.y)) or (isinstance(node1, Recharger) and isinstance(node2, Depot) and (node1.x == node2.x and node1.y == node2.y)):
                    continue
                if not node1 == node2:
                    distance = node1.distance_to(node2)
                    if isinstance(node1, Customer) and isinstance(node2, Customer) and (node1.demand+node2.demand) > self.model.vehicle.capacity:
                        continue
                    if node1.ready_time+node1.service_time+distance > node2.over_time:
                        continue
                    if node1.ready_time+node1.service_time+distance+node2.service_time+node2.distance_to(self.model.depot) > self.model.depot.over_time:
                        continue
                    if len(self.model.rechargers) != 0 and isinstance(node1, Customer) and isinstance(node2, Customer):
                        recharger1 = self.model.nearest_station[node1][0]
                        recharger2 = self.model.nearest_station[node2][0]
                        if self.model.vehicle.battery_cost_speed*(node1.distance_to(recharger1)+distance+node2.distance_to(recharger2)) > self.model.vehicle.max_battery:
                            continue
                    if distance == 0:
                        distance = 0.0000001
                    self.possible_arc[(node1, node2)] = distance

    def select_possible_arc(self, N: int) -> list:
        selected_arc = []
        keys = list(self.possible_arc.keys())
        while len(keys) > 0 and len(selected_arc) < N:
            values = [self.possible_arc[key] for key in keys]
            values = np.array(values)
            values = 1/values
            #values = values/np.sum(values)
            select = Util.wheel_select(values)
            selected_arc.append(keys[select])
            del keys[select]
        return selected_arc

    def update_penalty(self, S: Solution) -> None:
        self.penalty_update_flag[0].append(S.feasible_capacity(self.model))
        self.penalty_update_flag[1].append(S.feasible_time(self.model))
        self.penalty_update_flag[2].append(S.feasible_battery(self.model))
        for i in range(len(self.penalty)):
            if self.penalty_update_flag[i].count(False) == self.eta_penalty:
                self.penalty[i] += self.delta
                if self.penalty[i] > self.penalty_max[i]:
                    self.penalty[i] = self.penalty_max[i]
            elif self.penalty_update_flag[i].count(True) == self.eta_penalty:
                self.penalty[i] /= self.delta
                if self.penalty[i] < self.penalty_min[i]:
                    self.penalty[i] = self.penalty_min[i]

    def update_frequency(self, soloution: Solution) -> None:
        for which, route in enumerate(soloution.routes):
            for where in range(1, len(route.visit)-1):
                left, right = Operation.find_left_right_station(route, where)
                if (route.visit[where], which, left, right) in self.frequency:
                    self.frequency[(route.visit[where], which, left, right)] += 1
                else:
                    self.frequency[(route.visit[where], which, left, right)] = 1

    def random_create(self) -> Solution:
        x = random.uniform(self.model.get_map_bound()[0], self.model.get_map_bound()[1])
        y = random.uniform(self.model.get_map_bound()[2], self.model.get_map_bound()[3])
        choose = self.model.customers[:]
        choose.sort(key=lambda cus: Util.cal_angle_AoB((self.model.depot.x, self.model.depot.y), (x, y), (cus.x, cus.y)))
        routes = []
        building_route_visit = [self.model.depot, self.model.depot]

        choose_index = 0
        while choose_index < len(choose):
            allow_insert_place = list(range(1, len(building_route_visit)))

            while True:
                min_increase_dis = float('inf')
                decide_insert_place = None
                for insert_place in allow_insert_place:
                    increase_dis = choose[choose_index].distance_to(building_route_visit[insert_place-1])+choose[choose_index].distance_to(building_route_visit[insert_place])-building_route_visit[insert_place-1].distance_to(building_route_visit[insert_place])
                    if increase_dis < min_increase_dis:
                        decide_insert_place = insert_place
                if len(allow_insert_place) == 1:
                    break
                elif (isinstance(building_route_visit[decide_insert_place-1], Customer) and isinstance(building_route_visit[decide_insert_place], Customer)) and (building_route_visit[decide_insert_place-1].ready_time <= choose[choose_index].ready_time and choose[choose_index].ready_time <= building_route_visit[decide_insert_place].ready_time):
                    break
                elif (isinstance(building_route_visit[decide_insert_place-1], Customer) and not isinstance(building_route_visit[decide_insert_place], Customer)) and building_route_visit[decide_insert_place-1].ready_time <= choose[choose_index].ready_time:
                    break
                elif (not isinstance(building_route_visit[decide_insert_place-1], Customer) and isinstance(building_route_visit[decide_insert_place], Customer)) and choose[choose_index].ready_time <= building_route_visit[decide_insert_place]:
                    break
                elif not isinstance(building_route_visit[decide_insert_place-1], Customer) and not isinstance(building_route_visit[decide_insert_place], Customer):
                    break
                else:
                    allow_insert_place.remove(decide_insert_place)
                    continue

            building_route_visit.insert(decide_insert_place, choose[choose_index])

            try_route = Route(building_route_visit)
            if try_route.feasible_capacity(self.model.vehicle)[0] and try_route.feasible_battery(self.model.vehicle)[0]:
                del choose[choose_index]
            else:
                if len(routes) < self.model.max_vehicle-1:
                    del building_route_visit[decide_insert_place]
                    if len(building_route_visit) == 2:
                        choose_index += 1
                    else:
                        routes.append(Route(building_route_visit))
                        building_route_visit = [self.model.depot, self.model.depot]
                elif len(routes) == self.model.max_vehicle-1:
                    del choose[choose_index]

        routes.append(Route(building_route_visit[:-1]+choose+[self.model.depot]))

        return Solution(routes)

    def create_vns_neighbour(self, Rts: int, max: int) -> list:
        assert Rts >= 2 and max >= 1
        self.vns_neighbour = []
        for R in range(2, Rts+1):
            for m in range(1, max+1):
                self.vns_neighbour.append((R, m))

    def tabu_search(self, S: Solution) -> Solution:
        best_S = S
        #best_val = VNS_TS.get_objective(S, self.model, self.penalty)
        select_arc = self.select_possible_arc(100)
        tabu_list = {}
        for _ in range(self.eta_tabu):
            local_best_S = None
            local_best_act = None
            for arc in select_arc:
                for neighbor_opt in [Modification.two_opt_star_arc, Modification.relocate_arc, Modification.exchange_arc, Modification.stationInRe_arc]:
                    neighbor_sol, neighbor_act = neighbor_opt(self.model, S, *arc)
                    for sol in neighbor_sol:
                        assert sol.serve_all_customer(self.model)
                    for sol, act in zip(neighbor_sol, neighbor_act):
                        if tabu_list.get(act, 0) == 0:
                            if self.compare_better(sol, local_best_S):
                                local_best_S = sol
                                local_best_act = act
            for key in tabu_list:
                if tabu_list[key] >= 1:
                    tabu_list[key] -= 1
            tabu_list[local_best_act] = random.randint(self.nu_min, self.nu_max)
            if self.compare_better(local_best_S, best_S):
                best_S = local_best_S
            S = local_best_S
        return best_S

    def compare_better(self, solution1: Solution, solution2: Solution) -> bool:
        if solution2 is None:
            return True
        s1_val = VNS_TS.get_objective(solution1, self.model, self.penalty)
        s2_val = VNS_TS.get_objective(solution2, self.model, self.penalty)
        if solution1.feasible(self.model) and solution2.feasible(self.model):
            if len(solution1) < len(solution2) or (len(solution1) == len(solution2) and s1_val < s2_val):
                # if solution1.get_actual_routes() < solution2.get_actual_routes() or (solution1.get_actual_routes() == solution2.get_actual_routes() and s1_val < s2_val):
                return True
        elif solution1.feasible(self.model) and not solution2.feasible(self.model):
            return True
        elif not solution1.feasible(self.model) and solution2.feasible(self.model):
            return False
        elif not solution1.feasible(self.model) and not solution2.feasible(self.model):
            if s1_val < s2_val:
                return True
            else:
                return False

    def acceptSA_feas(self, S2: Solution, S: Solution, i) -> bool:
        S2_objective = VNS_TS.get_objective(S2, self.model, self.penalty)
        S_objective = VNS_TS.get_objective(S, self.model, self.penalty)
        if random.random() < self.SA_feas.probability(S2_objective, S_objective, i):
            return True
        return False

    def acceptSA_dist(self, S2: Solution, S: Solution, i) -> bool:
        S2_objective = VNS_TS.get_objective(S2, self.model, self.penalty)
        S_objective = VNS_TS.get_objective(S, self.model, self.penalty)
        if random.random() < self.SA_dist.probability(S2_objective, S_objective, i):
            return True
        return False

    def main(self) -> Solution:
        self.create_vns_neighbour(self.vns_neighbour_Rts, self.vns_neighbour_max)
        S = self.random_create()
        k = 0
        i = 0
        feasibilityPhase = True
        while feasibilityPhase or i < self.eta_dist:
            if self.compare_better(S, self.S_best):
                self.S_best = S
            self.update_penalty(S)

            print(i, S.feasible(self.model), len(S), VNS_TS.get_objective(S, self.model, self.penalty))

            S1 = Modification.cyclic_exchange(S, self.model, *self.vns_neighbour[k])
            S2 = self.tabu_search(S1)
            if self.compare_better(S2, S) or (feasibilityPhase and self.acceptSA_feas(S2, S, i)) or (not feasibilityPhase and self.acceptSA_dist(S2, S, i)):
                S = S2
                k = 0
            else:
                k = (k+1) % len(self.vns_neighbour)

            if feasibilityPhase:
                if not S.feasible(self.model):
                    if i == self.eta_feas:
                        S.add_empty_route(self.model)
                        i = -1
                else:
                    feasibilityPhase = False
                    i = -1
            i += 1

        return S


class DEMA(Evolution):
    # 构造属性
    model = None
    penalty = (15, 5, 10)
    maxiter_evo = 100
    size = 30
    infeasible_proportion = 0.25
    sigma = (1, 5, 10)
    theta = 0.7
    maxiter_tabu_mul = 4
    max_neighbour_mul = 3
    tabu_len = 4
    local_search_step = 10
    charge_modify_step = 14
    # 状态属性
    last_local_search = 0
    last_charge_modify = 0
    S_best = None
    min_cost = float('inf')

    def __init__(self, model: Model, **param) -> None:
        self.model = model
        for key, value in param.items():
            assert hasattr(self, key)
            setattr(self, key, value)
        assert self.size >= 4

    @ staticmethod
    def get_objective_route(route: Route, vehicle: Vehicle, penalty: tuple) -> float:
        return route.sum_distance()+penalty[0]*VNS_TS.penalty_capacity(route, vehicle)+penalty[1]*VNS_TS.penalty_time(route, vehicle)+penalty[2]*VNS_TS.penalty_battery(route, vehicle)

    @ staticmethod
    def get_objective(solution: Solution, model: Model, penalty: tuple) -> float:
        if solution.objective is None:
            ret = 0
            for route in solution.routes:
                ret += DEMA.get_objective_route(route, model.vehicle, penalty)
            solution.objective = ret
            return ret
        else:
            return solution.objective

    @ staticmethod
    def overlapping_degree(solution1: Solution, solution2: Solution) -> float:
        sol1arcs = []
        sol2arcs = []
        for route in solution1.routes:
            for i in range(len(route.visit)-1):
                sol1arcs.append((route.visit[i], route.visit[i+1]))
        for route in solution2.routes:
            for i in range(len(route.visit)-1):
                sol2arcs.append((route.visit[i], route.visit[i+1]))
        num = 0
        for arc in sol1arcs:
            if arc in sol2arcs:
                num += 2
        return num/(len(sol1arcs)+len(sol2arcs))

    @ staticmethod
    def overlapping_degree_population(solution: Solution, population: list) -> float:
        sum = 0
        for p in population:
            sum += DEMA.overlapping_degree(solution, p)
        return sum/len(population)

    def random_create(self) -> Solution:
        x = random.uniform(self.model.get_map_bound()[0], self.model.get_map_bound()[1])
        y = random.uniform(self.model.get_map_bound()[2], self.model.get_map_bound()[3])
        choose = self.model.customers[:]
        choose.sort(key=lambda cus: Util.cal_angle_AoB((self.model.depot.x, self.model.depot.y), (x, y), (cus.x, cus.y)))
        routes = []
        building_route_visit = [self.model.depot, self.model.depot]

        choose_index = 0
        while choose_index < len(choose):
            allow_insert_place = list(range(1, len(building_route_visit)))

            while True:
                min_increase_dis = float('inf')
                decide_insert_place = None
                for insert_place in allow_insert_place:
                    increase_dis = choose[choose_index].distance_to(building_route_visit[insert_place-1])+choose[choose_index].distance_to(building_route_visit[insert_place])-building_route_visit[insert_place-1].distance_to(building_route_visit[insert_place])
                    if increase_dis < min_increase_dis:
                        decide_insert_place = insert_place
                if len(allow_insert_place) == 1:
                    break
                elif (isinstance(building_route_visit[decide_insert_place-1], Customer) and isinstance(building_route_visit[decide_insert_place], Customer)) and (building_route_visit[decide_insert_place-1].ready_time <= choose[choose_index].ready_time and choose[choose_index].ready_time <= building_route_visit[decide_insert_place].ready_time):
                    break
                elif (isinstance(building_route_visit[decide_insert_place-1], Customer) and not isinstance(building_route_visit[decide_insert_place], Customer)) and building_route_visit[decide_insert_place-1].ready_time <= choose[choose_index].ready_time:
                    break
                elif (not isinstance(building_route_visit[decide_insert_place-1], Customer) and isinstance(building_route_visit[decide_insert_place], Customer)) and choose[choose_index].ready_time <= building_route_visit[decide_insert_place]:
                    break
                elif not isinstance(building_route_visit[decide_insert_place-1], Customer) and not isinstance(building_route_visit[decide_insert_place], Customer):
                    break
                else:
                    allow_insert_place.remove(decide_insert_place)
                    continue

            building_route_visit.insert(decide_insert_place, choose[choose_index])

            try_route = Route(building_route_visit)
            if try_route.feasible_capacity(self.model.vehicle)[0] and try_route.feasible_time(self.model.vehicle)[0]:
                # del choose[choose_index]
                choose_index += 1
            else:
                del building_route_visit[decide_insert_place]
                assert len(building_route_visit) != 2
                routes.append(Route(building_route_visit))
                building_route_visit = [self.model.depot, self.model.depot]

        routes.append(Route(building_route_visit))

        return Solution(routes)

    def initialization(self) -> list:
        population = []
        while len(population) < self.size:
            reroll = False
            times = 0
            sol = self.random_create()
            assert sol.serve_all_customer(self.model)
            while True:
                if times > 10:
                    reroll = True
                    break
                fes_dic = sol.feasible_detail(self.model)
                for _, value in fes_dic.items():
                    if value[1] == 'battery':
                        sol = Modification.charging_modification(sol, self.model)
                        assert sol.serve_all_customer(self.model)
                        times += 1
                        break
                    if value[1] == 'time':
                        sol = Modification.fix_time(sol, self.model)
                        assert sol.serve_all_customer(self.model)
                        times += 1
                        break
                else:
                    sol.renumber_id()
                    break
            if reroll:
                reroll = False
                continue
            population.append(sol)
        return population

    def ACO_GM(self, P: list) -> list:
        cross_score = [0.0, 0.0]
        cross_call_times = [0, 0]
        cross_weigh = [0.0, 0.0]

        fes_P = []
        infes_P = []
        for sol in P:
            if sol.feasible(self.model):
                fes_P.append(sol)
            else:
                infes_P.append(sol)
        fes_P.sort(key=lambda sol: DEMA.get_objective(sol, self.model, self.penalty))

        obj_value = []
        for sol in infes_P:
            overlapping_degree = DEMA.overlapping_degree_population(sol, P)
            objective = DEMA.get_objective(sol, self.model, self.penalty)
            obj_value.append([objective, overlapping_degree])
        infes_P = Util.pareto_sort(infes_P, obj_value)

        P = fes_P+infes_P
        choose = Util.binary_tournament(len(P))
        P_parent = []
        for i in choose:
            P_parent.append(P[i])
        P_child = []
        all_cost = [DEMA.get_objective(sol, self.model, self.penalty) for sol in P]
        while len(P_child) < self.size:
            # if len(P_child) == int((1-self.infeasible_proportion)*self.size):
            #    penalty_save = self.penalty
            #    self.penalty = (0, 0, 0)
            for i in range(2):
                if cross_call_times[i] != 0:
                    cross_weigh[i] = self.theta*np.pi/cross_call_times[i]+(1-self.theta)*cross_weigh[i]
            if cross_weigh[0] == 0 and cross_weigh[1] == 0:
                sel_prob = np.array([0.5, 0.5])
            else:
                sel_prob = np.array(cross_weigh)/np.sum(np.array(cross_weigh))
            sel = Util.wheel_select(sel_prob)

            if sel == 0:
                S_parent = random.choice(P_parent)
                S = Modification.ACO_GM_cross1(S_parent, self.model)
                assert S.serve_all_customer(self.model)
            elif sel == 1:
                S_parent, S2 = random.sample(P_parent, 2)
                S = Modification.ACO_GM_cross2(S_parent, S2, self.model)
                assert S.serve_all_customer(self.model)

            cross_call_times[sel] += 1
            cost = DEMA.get_objective(S, self.model, self.penalty)
            if cost < all(all_cost):
                cross_score[sel] += self.sigma[0]
            elif cost < DEMA.get_objective(S_parent, self.model, self.penalty):
                cross_score[sel] += self.sigma[1]
            else:
                cross_score[sel] += self.sigma[2]

            P_child.append(S)

        # self.penalty = penalty_save
        return P_child

    def ISSD(self, P: list, iter: int) -> list:
        SP1 = []
        SP2 = []
        for sol in P:
            if sol.feasible(self.model):
                SP1.append(sol)
            else:
                SP2.append(sol)
        SP1.sort(key=lambda sol: DEMA.get_objective(sol, self.model, self.penalty))
        obj_value = []
        for sol in SP2:
            overlapping_degree = DEMA.overlapping_degree_population(sol, P)
            objective = DEMA.get_objective(sol, self.model, self.penalty)
            obj_value.append([objective, overlapping_degree])
        SP2 = Util.pareto_sort(SP2, obj_value)
        #sp1up = int((iter/self.maxiter_evo)*self.size)
        sp1up = int((1-self.infeasible_proportion)*self.size)
        sp2up = self.size-sp1up
        P = SP1[:sp1up]+SP2[:sp2up]
        SP1 = SP1[sp1up:]
        SP2 = SP2[sp2up:]
        for sol in SP1:
            if len(P) < self.size:
                P.append(sol)
        for sol in SP2:
            if len(P) < self.size:
                P.append(sol)
        assert len(P) == self.size

        # for sol in P:
        #    sol.clear_status()

        return P

    def tabu_search_vnsts(self, solution: Solution) -> Solution:
        if getattr(self, 'vnsts', None) is None:
            self.vnsts = VNS_TS(self.model)
            self.vnsts.penalty = self.penalty
        sol = self.vnsts.tabu_search(solution)
        return sol

    def tabu_search_abandon(self, solution: Solution, iter_num: int, neighbor_num: int) -> Solution:
        best_sol = solution
        best_val = DEMA.get_objective(solution, self.model, self.penalty)
        tabu_list = {}
        # delta = collections.deque([float('inf')]*10, maxlen=10)
        for iter in range(iter_num):
            print('tabu {} {}'.format(iter, best_val))
            actions = []
            while len(actions) < neighbor_num:
                act = random.choice(['exchange', 'relocate', 'two-opt', 'stationInRe'])
                if act == 'exchange':
                    target = Modification.exchange_choose(solution)
                    if ('exchange', *target) not in actions:
                        actions.append(('exchange', *target))
                elif act == 'relocate':
                    target = Modification.relocate_choose(solution)
                    if ('relocate', *target) not in actions:
                        actions.append(('relocate', *target))
                elif act == 'two-opt':
                    target = Modification.two_opt_choose(solution)
                    if ('two-opt', *target) not in actions:
                        actions.append(('two-opt', *target))
                elif act == 'stationInRe':
                    target = Modification.stationInRe_choose(solution, self.model)
                    if ('stationInRe', *target) not in actions:
                        actions.append(('stationInRe', *target))
            local_best_sol = solution
            local_best_val = DEMA.get_objective(solution, self.model, self.penalty)
            local_best_action = (None,)
            for action in actions:
                tabu_status = tabu_list.get(action, 0)
                if tabu_status == 0:
                    if action[0] == 'exchange':
                        try_sol = Modification.exchange_action(solution, *action[1:])
                    elif action[0] == 'relocate':
                        try_sol = Modification.relocate_action(solution, *action[1:])
                    elif action[0] == 'two-opt':
                        try_sol = Modification.two_opt_action(solution, *action[1:])
                    elif action[0] == 'stationInRe':
                        try_sol = Modification.stationInRe_action(solution, *action[1:])
                    try_val = DEMA.get_objective(try_sol, self.model, self.penalty)
                    if try_val < local_best_val:
                        local_best_sol = try_sol
                        local_best_val = try_val
                        local_best_action = action
            for key in tabu_list:
                if tabu_list[key] > 0:
                    tabu_list[key] -= 1
            if local_best_action[0] == 'exchange':
                tabu_list[('exchange', *local_best_action[1:])] = self.tabu_len
                tabu_list[('exchange', *local_best_action[3:5], *local_best_action[1:3])] = self.tabu_len
            elif local_best_action[0] == 'relocate':
                tabu_list[('relocate', *local_best_action[1:])] = self.tabu_len
            elif local_best_action[0] == 'two-opt':
                tabu_list[('two-opt', *local_best_action[1:])] = self.tabu_len
                tabu_list[('two-opt', local_best_action[1], local_best_action[3], local_best_action[2])] = self.tabu_len
            if local_best_val < best_val:
                best_sol = local_best_sol
                best_val = local_best_val

            solution = local_best_sol

            # delta.append(DEMA.get_objective(solution, self.model, self.penalty)-local_best_val)
            # should_break = True
            # for i in delta:
            #    if i > 0.00001:
            #        should_break = False
            #    break
            # if should_break:
            #    break

        return best_sol

    def MVS(self, P: list, iter: int) -> list:
        self.last_local_search += 1
        self.last_charge_modify += 1
        if self.last_local_search >= self.local_search_step:
            retP = []
            for i, sol in enumerate(P):
                print(iter, 'tabu', i)
                retP.append(self.tabu_search_vnsts(sol))
            self.last_local_search = 0
            return retP
        elif self.last_charge_modify >= self.charge_modify_step:
            retP = []
            for i, sol in enumerate(P):
                print(iter, 'charge', i)
                retP.append(Modification.charging_modification(sol, self.model))
            self.last_charge_modify = 0
            return retP
        return P

    def update_S(self, P: list) -> None:
        for S in P:
            if S.feasible(self.model):
                cost = DEMA.get_objective(S, self.model, self.penalty)
                num = len(S.routes)
                # cost = S.sum_distance()
                if self.S_best is None:
                    self.S_best = S
                    self.min_cost = cost
                elif not self.S_best is None and num < len(self.S_best.routes):
                    self.S_best = S
                    self.min_cost = cost
                elif not self.S_best is None and num == len(self.S_best.routes):
                    if cost < self.min_cost:
                        self.S_best = S
                        self.min_cost = cost

    def main(self, icecube: list = None) -> tuple:
        if icecube is None:
            P = self.initialization()
        else:
            self.model, self.S_best, self.min_cost, P = icecube
        self.update_S(P)
        for iter in range(self.maxiter_evo):
            print(iter, len(self.S_best), self.min_cost)
            P_child = self.ACO_GM(P)
            P = self.ISSD(P+P_child, iter)
            P = self.MVS(P, iter)
            self.update_S(P)
            self.P = P
        return self.S_best, self.min_cost

    def freeze(self) -> list:
        return [self.model, self.S_best, self.min_cost, self.P]
