import {createButailingGroups} from "./butailing.js";
import {createCommonSearchGroups} from "./common.js";
import {createDian115Groups} from "./dian115.js";
import {createHdhiveGroups} from "./hdhive.js";
import {createJuyingGroups} from "./juying.js";
import {createPansouGroups} from "./pansou.js";
import {createSeedhubGroups} from "./seedhub.js";
import {createPinglianGroups} from "./pinglian.js";

export function createSearchSection(resourceTypeItems, options = {}) {
    return {
        value: "search",
        title: "搜索设置",
        icon: "mdi-magnify",
        subtabs: [
            {value: "common", title: "通用", icon: "mdi-tune"},
            {value: "pansou", title: "PanSou", icon: "mdi-magnify-scan"},
            {value: "hdhive", title: "HDHive", icon: "mdi-hexagon-multiple-outline"},
            {value: "dian115", title: "Dian115", icon: "mdi-cloud-search"},
            {value: "juying", title: "聚影", icon: "mdi-movie-search-outline"},
            {value: "pinglian", title: "盘链", icon: "mdi-link-variant"},
            {value: "seedhub", title: "SeedHub", icon: "mdi-seed-outline"},
            {value: "butailing", title: "不太灵", icon: "mdi-magnet"},
        ],
        groups: [
            ...createCommonSearchGroups(resourceTypeItems),
            ...createPansouGroups(options.pansou || {}),
            ...createJuyingGroups(options),
            ...createSeedhubGroups(),
            ...createButailingGroups(),
            ...createHdhiveGroups(options),
            ...createDian115Groups(options),
            ...createPinglianGroups(options),
        ],
    };
}
